import os
import torch

from transformers import BitsAndBytesConfig
from transformers import (
  AutoTokenizer, 
  AutoModelForCausalLM, 
  BitsAndBytesConfig,
  pipeline,
)

from langchain.chains.llm import LLMChain
from langchain.retrievers import EnsembleRetriever
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface.embeddings import HuggingFaceEmbeddings
from langchain_huggingface.llms import HuggingFacePipeline
from langchain.prompts import PromptTemplate
from langchain.schema.runnable import RunnablePassthrough
from langchain_community.vectorstores import FAISS
from langchain_community.retrievers import BM25Retriever
from langchain.retrievers.multi_query import MultiQueryRetriever
from langchain.retrievers.multi_query import LineListOutputParser

from langchain_community.document_loaders import PyPDFLoader
from langchain_community.document_loaders import PyPDFDirectoryLoader
from langchain_community.document_loaders import JSONLoader
from langchain_community.document_loaders import DirectoryLoader
from langchain_community.document_loaders import Docx2txtLoader
from langchain_community.document_loaders import UnstructuredExcelLoader
from langchain_community.document_loaders import UnstructuredPowerPointLoader

# Make documents look a bit better than default
def formatDocuments(docs):
    doc_strings = []
    for doc in docs:
        metadata_string = ", ".join([f"{md}: {doc.metadata[md]}" for md in doc.metadata.keys()])
        doc_strings.append(f"Content: {doc.page_content}\nMetadata: {metadata_string}")
    return "\n\n".join(doc_strings)

def getFilenames(docs):
    return [doc.metadata['source'] for doc in docs if 'source' in doc.metadata]

# Capture the context of the retriever
class CaptureContext(RunnablePassthrough):
    def __init__(self):
        self.captured_context = None
    
    def run(self, input_data):
        self.captured_context = input_data['context']
        return input_data

class RAGHelper:
    def __init__(self, logger):
        # Set up the LLM
        use_4bit = True
        bnb_4bit_compute_dtype = "float16"
        bnb_4bit_quant_type = "nf4"

        use_nested_quant = False
        compute_dtype = getattr(torch, bnb_4bit_compute_dtype)

        bnb_config = BitsAndBytesConfig(
            load_in_4bit=use_4bit,
            bnb_4bit_quant_type=bnb_4bit_quant_type,
            bnb_4bit_compute_dtype=compute_dtype,
            bnb_4bit_use_double_quant=use_nested_quant,
        )

        if compute_dtype == torch.float16 and use_4bit:
            major, _ = torch.cuda.get_device_capability()
            if major >= 8:
                logger.debug("=" * 80)
                logger.debug("Your GPU supports bfloat16: accelerate training with bf16=True")
                logger.debug("=" * 80)
        
        llm_model = os.getenv('llm_model')
        trust_remote_code = os.getenv('trust_remote_code')
        self.tokenizer = AutoTokenizer.from_pretrained(llm_model, trust_remote_code=trust_remote_code)
        model = AutoModelForCausalLM.from_pretrained(
            llm_model,
            quantization_config=bnb_config,
            trust_remote_code=trust_remote_code,
        )

        text_generation_pipeline = pipeline(
            model=model,
            tokenizer=self.tokenizer,
            task="text-generation",
            temperature=float(os.getenv('temperature')),
            repetition_penalty=float(os.getenv('repetition_penalty')),
            return_full_text=True,
            max_new_tokens=int(os.getenv('max_new_tokens')),
        )

        self.llm = HuggingFacePipeline(pipeline=text_generation_pipeline)

        # Set up embedding handling for vector store
        if os.getenv('force_cpu'):
            model_kwargs = {
                'device': 'cpu'
            }
        else:
            model_kwargs = {
                'device': 'cuda'
            }
        self.embeddings = HuggingFaceEmbeddings(
            model_name=os.getenv('embedding_model'),
            model_kwargs=model_kwargs
        )

        # Load the data
        self.loadData()

        # Create the RAG chain for determining if we need to fetch new documents
        rag_thread = [{
            'role': 'system', 'content': os.getenv('rag_fetch_new_instruction')
        }, {
            'role': 'user', 'content': os.getenv('rag_fetch_new_question')
        }]
        rag_prompt_template = self.tokenizer.apply_chat_template(rag_thread, tokenize=False)
        rag_prompt = PromptTemplate(
            input_variables=["question"],
            template=rag_prompt_template,
        )
        rag_llm_chain = LLMChain(llm=self.llm, prompt=rag_prompt)
        self.rag_fetch_new_chain = (
            {"question": RunnablePassthrough()} |
            rag_llm_chain
        )

        # Also create the rewrite loop LLM chain, if need be
        self.rewrite_ask_chain = None
        self.rewrite_chain = None
        if os.getenv("use_rewrite_loop"):
            # First the chain to ask the LLM if a rewrite would be required
            rewrite_ask_thread = [{
                'role': 'system', 'content': os.getenv('rewrite_query_instruction')
            }, {
                'role': 'user', 'content': os.getenv('rewrite_query_question')
            }]
            rewrite_ask_prompt_template = self.tokenizer.apply_chat_template(rewrite_ask_thread, tokenize=False)
            rewrite_ask_prompt = PromptTemplate(
                input_variables=["context", "question"],
                template=rewrite_ask_prompt_template,
            )
            rewrite_ask_llm_chain = LLMChain(llm=self.llm, prompt=rewrite_ask_prompt)
            self.rewrite_ask_chain = (
                {"context": self.ensemble_retriever | formatDocuments, "question": RunnablePassthrough()} |
                rewrite_ask_llm_chain
            )

            # Next the chain to ask the LLM for the actual rewrite(s)
            # First ask the LLM if a rewrite would be required
            rewrite_thread = [{
                'role': 'user', 'content': os.getenv('rewrite_query_question')
            }]
            rewrite_prompt_template = self.tokenizer.apply_chat_template(rewrite_thread, tokenize=False)
            rewrite_prompt = PromptTemplate(
                input_variables=["question"],
                template=rewrite_prompt_template,
            )
            rewrite_llm_chain = LLMChain(llm=self.llm, prompt=rewrite_prompt)
            self.rewrite_chain = (
                {"question": RunnablePassthrough()} |
                rewrite_llm_chain
            )

    # Loads the data and chunks it into an ensemble retriever
    def loadData(self):
        # Load PDF files if need be
        docs = []
        data_dir = os.getenv('data_directory')
        file_types = os.getenv("file_types").split(",")
        if "pdf" in file_types:
            loader = PyPDFDirectoryLoader(data_dir)
            docs = docs + loader.load()
        # Load JSON
        if "json" in file_types:
            text_content = True
            if os.getenv("json_text_content").lower() == 'false':
                text_content = False
            loader_kwargs = {
                'jq_schema': os.getenv("json_schema"),
                'text_content': text_content
            }
            loader = DirectoryLoader(
                path=data_dir,
                glob="*.json",
                loader_cls=JSONLoader,
                loader_kwargs=loader_kwargs,
            )
            docs = docs + loader.load()
        # Load MS Word
        if "docx" in file_types:
            loader = DirectoryLoader(
                path=data_dir,
                glob="*.docx",
                loader_cls=Docx2txtLoader,
            )
            docs = docs + loader.load()
        # Load MS Excel
        if "xlsx" in file_types:
            loader = DirectoryLoader(
                path=data_dir,
                glob="*.xlsx",
                loader_cls=UnstructuredExcelLoader,
            )
            docs = docs + loader.load()
        # Load MS PPT
        if "pptx" in file_types:
            loader = DirectoryLoader(
                path=data_dir,
                glob="*.pptx",
                loader_cls=UnstructuredPowerPointLoader,
            )
            docs = docs + loader.load()

        #if os.getenv('splitter') == 'RecursiveCharacterTextSplitter':
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=int(os.getenv('chunk_size')),
            chunk_overlap=int(os.getenv('chunk_overlap')),
            length_function=len,
            keep_separator=False,
            is_separator_regex=True,
            separators=[
                "\n \n",
                "\n\n",
                "\n",
                "[.!?]",
                " ",
                ",",
                "\u200b",  # Zero-width space
                "\uff0c",  # Fullwidth comma
                "\u3001",  # Ideographic comma
                "\uff0e",  # Fullwidth full stop
                "\u3002",  # Ideographic full stop
                "",
            ],
        )

        self.chunked_documents = self.text_splitter.split_documents(docs)

        vector_store_path = os.getenv('vector_store_path')
        if os.path.exists(vector_store_path):
            self.db = FAISS.load_local(vector_store_path, self.embeddings, allow_dangerous_deserialization=True)
        else:
            # Load chunked documents into the FAISS index
            self.db = FAISS.from_documents(self.chunked_documents, self.embeddings)
            self.db.save_local(vector_store_path)

        # Now the BM25 retriever
        bm25_retriever = BM25Retriever.from_texts(
            [x.page_content for x in self.chunked_documents],
            metadatas=[x.metadata for x in self.chunked_documents]
        )
        # Set up the vector retriever
        retriever=self.db.as_retriever(
            search_type="mmr", search_kwargs = {'k': int(os.getenv("vector_store_k"))}
        )

        # Now combine them to do hybrid retrieval
        self.ensemble_retriever = EnsembleRetriever(
            retrievers=[bm25_retriever, retriever], weights=[0.5, 0.5]
        )

    def handle_rewrite(self, user_query):
        # Check if we even need to rewrite or not
        if os.getenv("use_rewrite_loop"):
            # Ask the LLM if we need to rewrite
            response = self.rewrite_ask_chain.invoke(user_query)
            if response['text'].lower().endswith('yes'):
                # Start the rewriting into different alternatives
                response = self.rewrite_chain.invoke(user_query)
                # Take out the alternatives
                term_symbol = self.tokenizer.eos_token
                end_string = f"{term_symbol}assistant\n\n"
                reply = response['text'][response['text'].find(end_string)+len(end_string):]
                # Show be split by newlines
                return reply
            else:
                # We do not need to rewrite
                return user_query
        else:
            return user_query

    # Main function to handle user interaction
    def handle_user_interaction(self, user_query, history):
        if len(history) == 0:
            fetch_new_documents = True
        else:
            # Prompt for LLM
            response = self.rag_fetch_new_chain.invoke(user_query)
            if response['text'].lower().endswith('yes'):
                fetch_new_documents = True
            else:
                fetch_new_documents = False

        # Create prompt template based on whether we have history or not
        thread = []
        if len(history) == 0:
            thread.append({
                'role': 'system', 'content': os.getenv('rag_instruction')})
            thread.append({
                'role': 'user', 'content': os.getenv('rag_question_initial')
            })
        else:
            thread.append({
                'role': 'user', 'content': os.getenv('rag_question_followup')
            })

        # Determine input variables
        if fetch_new_documents:
            input_variables = ["context", "question"]
        else:
            input_variables = ["question"]

        prompt_template = history.replace("{", "(").replace("}", ")") + '\n' + self.tokenizer.apply_chat_template(thread, tokenize=False)

        # Create prompt from prompt template
        prompt = PromptTemplate(
            input_variables=input_variables,
            template=prompt_template,
        )

        # Create llm chain
        llm_chain = LLMChain(llm=self.llm, prompt=prompt)
        if fetch_new_documents:
            # Rewrite the question if needed
            user_query = self.handle_rewrite(user_query)
            rag_chain = (
                {"docs": self.ensemble_retriever | getFilenames, "context": self.ensemble_retriever | formatDocuments, "question": RunnablePassthrough()} |
                llm_chain
            )
        else:
            rag_chain = (
                {"question": RunnablePassthrough()} |
                llm_chain
            )

        # Invoke RAG pipeline
        reply = rag_chain.invoke(user_query)
        return reply

    def addDocument(self, filename):
        if filename.lower().endswith() == 'pdf':
            doc = PyPDFLoader(filename).load()
        if filename.lower().endswith() == 'json':
            doc = JSONLoader(
                file_path = filename,
                jq_schema = os.getenv("json_schema"),
                text_content = os.getenv("json_text_content"),
            ).load()
        if filename.lower().endswith() == 'docx':
            doc = Docx2txtLoader(filename).load()
        if filename.lower().endswith() == 'xlsx':
            doc = UnstructuredExcelLoader(filename).load()
        if filename.lower().endswith() == 'pptx':
            doc = UnstructuredPowerPointLoader(filename).load()

        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=int(os.getenv('chunk_size')),
            chunk_overlap=int(os.getenv('chunk_overlap')),
            length_function=len,
            keep_separator=False,
            is_separator_regex=True,
            separators=[
                "\n \n",
                "\n\n",
                "\n",
                "[.!?]",
                " ",
                ",",
                "\u200b",  # Zero-width space
                "\uff0c",  # Fullwidth comma
                "\u3001",  # Ideographic comma
                "\uff0e",  # Fullwidth full stop
                "\u3002",  # Ideographic full stop
                "",
            ],
        )
        new_chunks = self.text_splitter.split_documents(doc)

        # Add to FAISS
        self.db.add_documents(new_chunks)
        self.db.save_local(os.getenv('vector_store_path'))

        # Add to BM25
        self.chunked_documents = [x.page_content for x in self.chunked_documents] + [x.page_content for x in new_chunks]
        bm25_retriever = BM25Retriever.from_texts(
            [x.page_content for x in self.chunked_documents],
            metadatas=[x.metadata for x in self.chunked_documents]
        )

        # Update full retriever too
        retriever = self.db.as_retriever(search_type="mmr", search_kwargs = {'k': int(os.getenv('vector_store_k'))})
        self.ensemble_retriever = EnsembleRetriever(
            retrievers=[bm25_retriever, retriever], weights=[0.5, 0.5]
        )