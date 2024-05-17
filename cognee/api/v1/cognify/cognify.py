import asyncio
import os
from uuid import uuid4
from typing import List, Union
import logging
import instructor
import nltk
from openai import OpenAI
from nltk.corpus import stopwords

from cognee.api.v1.prune import prune
from cognee.config import Config
from cognee.infrastructure.data.chunking.LangchainChunkingEngine import LangchainChunkEngine
from cognee.infrastructure.databases.vector.embeddings.DefaultEmbeddingEngine import LiteLLMEmbeddingEngine
from cognee.modules.cognify.graph.add_data_chunks import add_data_chunks
from cognee.modules.cognify.graph.add_document_node import add_document_node
from cognee.modules.cognify.graph.add_classification_nodes import add_classification_nodes
from cognee.modules.cognify.graph.add_cognitive_layer_graphs import add_cognitive_layer_graphs
from cognee.modules.cognify.graph.add_summary_nodes import add_summary_nodes
from cognee.modules.cognify.graph.add_node_connections import group_nodes_by_layer, \
    graph_ready_output, connect_nodes_in_graph
from cognee.modules.cognify.llm.resolve_cross_graph_references import resolve_cross_graph_references
from cognee.infrastructure.databases.graph.get_graph_client import get_graph_client
from cognee.modules.cognify.graph.add_cognitive_layers import add_cognitive_layers
# from cognee.modules.cognify.graph.initialize_graph import initialize_graph
from cognee.infrastructure.files.utils.guess_file_type import guess_file_type, FileTypeException
from cognee.infrastructure.files.utils.extract_text_from_file import extract_text_from_file
from cognee.infrastructure import infrastructure_config
from cognee.modules.data.get_content_categories import get_content_categories
from cognee.modules.data.get_content_summary import get_content_summary
from cognee.modules.data.get_cognitive_layers import get_cognitive_layers
from cognee.modules.data.get_layer_graphs import get_layer_graphs
from cognee.modules.topology.topology import TopologyEngine
from cognee.shared.GithubClassification import CodeContentPrediction
from cognee.shared.data_models import ChunkStrategy
from cognee.utils import send_telemetry


config = Config()
config.load()

aclient = instructor.patch(OpenAI())

USER_ID = "default_user"

logger = logging.getLogger("cognify")

async def cognify(datasets: Union[str, List[str]] = None):
    """This function is responsible for the cognitive processing of the content."""
    # Has to be loaded in advance, multithreading doesn't work without it.
    nltk.download("stopwords", quiet=True)
    stopwords.ensure_loaded()

    graph_db_type = infrastructure_config.get_config()["graph_engine"]

    graph_client = await get_graph_client(graph_db_type)

    db_engine = infrastructure_config.get_config()["database_engine"]

    if datasets is None or len(datasets) == 0:
        datasets = db_engine.get_datasets()

    awaitables = []

    # datasets is a list of dataset names
    if isinstance(datasets, list):
        for dataset in datasets:
            awaitables.append(cognify(dataset))

        graphs = await asyncio.gather(*awaitables)
        return graphs[0]

    added_datasets = db_engine.get_datasets()

    dataset_files = []
    # datasets is a dataset name string
    dataset_name = datasets.replace(".", "_").replace(" ", "_")
    for added_dataset in added_datasets:
        if dataset_name in added_dataset:
            dataset_files.append((added_dataset, db_engine.get_files_metadata(added_dataset)))

    # print("dataset_files", dataset_files)


    data_chunks = {}

    chunk_engine = infrastructure_config.get_config()["chunk_engine"]
    chunk_strategy = infrastructure_config.get_config()["chunk_strategy"]
    async def process_batch(files_batch):
        for dataset_name, file_metadata in files_batch:
            with open(file_metadata["file_path"], "rb") as file:
                try:
                    document_id = await add_document_node(
                        graph_client,
                        parent_node_id = f"DefaultGraphModel__{USER_ID}",
                        document_metadata = file_metadata,
                    )

                    file_type = guess_file_type(file)
                    text = extract_text_from_file(file, file_type)
                    if text is None:
                        text = "empty file"
                    subchunks = chunk_engine.chunk_data(chunk_strategy, text, config.chunk_size, config.chunk_overlap)

                    if dataset_name not in data_chunks:
                        data_chunks[dataset_name] = []

                    for subchunk in subchunks:
                        data_chunks[dataset_name].append(dict(document_id = document_id, chunk_id = str(uuid4()), text = subchunk))

                except FileTypeException:
                    logger.warning("File (%s) has an unknown file type. We are skipping it.", file_metadata["id"])

        added_chunks = await add_data_chunks(data_chunks)


        await asyncio.gather(
            *[process_text(chunk["collection"], chunk["chunk_id"], chunk["text"], chunk["file_metadata"]) for chunk in
              added_chunks]
        )

    batch_size = 20
    file_count = 0
    files_batch = []

    for (dataset_name, files) in dataset_files:
        for file_metadata in files:
            files_batch.append((dataset_name, file_metadata))
            file_count += 1

            if file_count >= batch_size:
                await process_batch(files_batch)
                files_batch = []
                file_count = 0

    # Process any remaining files in the last batch
    if files_batch:
        await process_batch(files_batch)

    return graph_client.graph

    #
    # for (dataset_name, files) in dataset_files:
    #     for file_metadata in files:
    #         with open(file_metadata["file_path"], "rb") as file:
    #             try:
    #                 file_type = guess_file_type(file)
    #                 text = extract_text_from_file(file, file_type)
    #                 if text is None:
    #                     text = "empty file"
    #                 subchunks = chunk_engine.chunk_data(chunk_strategy, text, config.chunk_size, config.chunk_overlap)
    #
    #                 if dataset_name not in data_chunks:
    #                     data_chunks[dataset_name] = []
    #
    #                 for subchunk in subchunks:
    #                     data_chunks[dataset_name].append(dict(text = subchunk, chunk_id = str(uuid4()), file_metadata = file_metadata))
    #             except FileTypeException:
    #                 logger.warning("File (%s) has an unknown file type. We are skipping it.", file_metadata["id"])
    #
    #
    #
    #
    # added_chunks: list[tuple[str, str, dict]] = await add_data_chunks(data_chunks)
    #
    # await asyncio.gather(
    #     *[process_text(chunk["collection"], chunk["chunk_id"], chunk["text"], chunk["file_metadata"]) for chunk in added_chunks]
    # )
    #
    # return graph_client.graph

async def process_text(chunk_collection: str, chunk_id: str, input_text: str, file_metadata: dict):
    print(f"Processing chunk ({chunk_id}) from document ({file_metadata['id']}).")

    graph_client = await get_graph_client(infrastructure_config.get_config()["graph_engine"])

    graph_topology = infrastructure_config.get_config()["graph_topology"]

    print("got here")


    document_id = await add_document_node(
        graph_client,
        parent_node_id = f"{file_metadata['name']}.{file_metadata['extension']}", #make a param of defaultgraph model to make sure when user passes his stuff, it doesn't break pipeline
        document_metadata = file_metadata,
    )
    # print("got here2")
    # await add_label_nodes(graph_client, document_id, chunk_id, file_metadata["keywords"].split("|"))

    # classified_categories = await get_content_categories(input_text)
    #
    # print("classified_categories", classified_categories)
    # await add_classification_nodes(
    #      graph_client,
    #      parent_node_id = document_id,
    #      categories = classified_categories,
    #  )

    classified_categories= [{'data_type': 'text', 'category_name': 'Source code in various programming languages'}]


    await asyncio.gather(
        *[process_text(chunk["document_id"], chunk["chunk_id"], chunk["collection"], chunk["text"]) for chunk in added_chunks]
    )

    return graph_client.graph
#
# async def process_text(document_id: str, chunk_id: str, chunk_collection: str, input_text: str):
#     raw_document_id = document_id.split("__")[-1]
#
#     print(f"Processing chunk ({chunk_id}) from document ({raw_document_id}).")
#
#     graph_client = await get_graph_client(infrastructure_config.get_config()["graph_engine"])
#
#     classified_categories = await get_content_categories(input_text)
#     await add_classification_nodes(
#         graph_client,
#         parent_node_id = document_id,
#         categories = classified_categories,
#     )
# >>>>>>> origin/main
#
#     print(f"Chunk ({chunk_id}) classified.")
#
#     # print("document_id", document_id)
#     #
#     # content_summary = await get_content_summary(input_text)
#     # await add_summary_nodes(graph_client, document_id, content_summary)
#
#     print(f"Chunk ({chunk_id}) summarized.")
#     #
#     cognitive_layers = await get_cognitive_layers(input_text, classified_categories)
#     cognitive_layers = (await add_cognitive_layers(graph_client, document_id, cognitive_layers))[:2]
#     #
#     layer_graphs = await get_layer_graphs(input_text, cognitive_layers)
#     await add_cognitive_layer_graphs(graph_client, chunk_collection, chunk_id, layer_graphs)
#
# <<<<<<< HEAD
#     print("got here 4444")
    #
    # if infrastructure_config.get_config()["connect_documents"] is True:
    #     db_engine = infrastructure_config.get_config()["database_engine"]
    #     relevant_documents_to_connect = db_engine.fetch_cognify_data(excluded_document_id = file_metadata["id"])
    #
    #     list_of_nodes = []
    #
    #     relevant_documents_to_connect.append({
    #         "layer_id": document_id,
    #     })
    #
    #     for document in relevant_documents_to_connect:
    #         node_descriptions_to_match = await graph_client.extract_node_description(document["layer_id"])
    #         list_of_nodes.extend(node_descriptions_to_match)
    #
    #     nodes_by_layer = await group_nodes_by_layer(list_of_nodes)
    #
    #     results = await resolve_cross_graph_references(nodes_by_layer)
    #
    #     relationships = graph_ready_output(results)
    #
    #     await connect_nodes_in_graph(
    #         graph_client,
    #         relationships,
    #         score_threshold = infrastructure_config.get_config()["intra_layer_score_treshold"]
    #     )
    #
    # send_telemetry("cognee.cognify")
    #
    # print(f"Chunk ({chunk_id}) cognified.")
# =======
#     if infrastructure_config.get_config()["connect_documents"] is True:
#         db_engine = infrastructure_config.get_config()["database_engine"]
#         relevant_documents_to_connect = db_engine.fetch_cognify_data(excluded_document_id = raw_document_id)
#
#         list_of_nodes = []
#
#         relevant_documents_to_connect.append({
#             "layer_id": document_id,
#         })
#
#         for document in relevant_documents_to_connect:
#             node_descriptions_to_match = await graph_client.extract_node_description(document["layer_id"])
#             list_of_nodes.extend(node_descriptions_to_match)
#
#         nodes_by_layer = await group_nodes_by_layer(list_of_nodes)
#
#         results = await resolve_cross_graph_references(nodes_by_layer)
#
#         relationships = graph_ready_output(results)
#
#         await connect_nodes_in_graph(
#             graph_client,
#             relationships,
#             score_threshold = infrastructure_config.get_config()["intra_layer_score_treshold"]
#         )
#
#     send_telemetry("cognee.cognify")
#
#     print(f"Chunk ({chunk_id}) cognified.")
# >>>>>>> origin/main


if __name__ == "__main__":

    async def test():
        # await prune.prune_system()
        # #
        # from cognee.api.v1.add import add
        # data_directory_path = os.path.abspath("../../../.data")
        # # print(data_directory_path)
        # # config.data_root_directory(data_directory_path)
        # # cognee_directory_path = os.path.abspath("../.cognee_system")
        # # config.system_root_directory(cognee_directory_path)
        #
        # await add("data://" +data_directory_path, "example")

        infrastructure_config.set_config( {"chunk_engine": LangchainChunkEngine() , "chunk_strategy": ChunkStrategy.CODE,'embedding_engine': LiteLLMEmbeddingEngine() })
        from cognee.shared.SourceCodeGraph import SourceCodeGraph
        from cognee.api.v1.config import config

        config.set_graph_model(SourceCodeGraph)

        config.set_classification_model(CodeContentPrediction)

        graph = await cognify()
        #
        from cognee.utils import render_graph

        await render_graph(graph, include_color=True, include_nodes=False, include_size=False)

    import asyncio
    asyncio.run(test())
