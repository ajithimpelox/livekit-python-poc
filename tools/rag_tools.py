import logging
import re

from typing import Dict, Any
from langchain_openai import OpenAIEmbeddings
from langchain_pinecone import PineconeVectorStore

# Set up logger
logger = logging.getLogger(__name__)

async def get_rag_information_from_vector_store(
    namespace: str,
    index_name: str,
    message: str,
    top_k: int = 1
) -> Dict[str, Any]:
    """
    Get relevant data from vector store
    
    Args:
        namespace: namespace of the vector store
        index_name: index of the vector store
        message: message sent by the user
        top_k: number of top results to return
        
    Returns:
        Dictionary containing results, page info, and whether query is page-specific
    """
    embeddings = OpenAIEmbeddings(
        model='text-embedding-3-small'
    )
    
    try:        
        vector_store: PineconeVectorStore = PineconeVectorStore.from_existing_index(
            index_name=index_name,
            embedding=embeddings,
            namespace=namespace
        )
        
        # Check if user is asking for a specific page
        page_match = re.search(r'(?:page|pg)(?:\s+(?:no|number|num))?\s*(\d+)', message, re.IGNORECASE)
        is_page_specific_query = page_match is not None
        
        if is_page_specific_query:
            requested_page = int(page_match.group(1))
            query_embedding = await embeddings.aembed_query(message)
            all_results = await vector_store.asimilarity_search_by_vector_with_score(
                embedding=query_embedding,
                k=20  # Need more results to find all chunks from the specific page
            )
            
            page_results = []
            for doc, score in all_results:
                doc_page = doc.metadata.get('page')
                if doc_page == requested_page or doc_page == str(requested_page):
                    page_results.append((doc, score))
            
            return {
                'results': page_results,
                'page': requested_page,
                'is_page_specific': True
            }
        else:
            query_embedding = await embeddings.aembed_query(message)
            results = await vector_store.asimilarity_search_by_vector_with_score(
                embedding=query_embedding,
                k=top_k
            )
            
            first_page = None
            if results and len(results) > 0:
                first_page = results[0][0].metadata.get('page')
            
            return {
                'results': results,
                'page': first_page,
                'is_page_specific': False
            }
            
    except Exception as error:
        error_msg = 'Error retrieving data from vector store'
        logger.error(
            f"{error_msg}: {str(error)} - Namespace: {namespace}, Index: {index_name}, Message: {message[:100]}"
        )
        raise error

