import os
import json
import logging
import requests
import dataclasses

from typing import List

from urllib.parse import quote, unquote

# Encode text to make it safe for AI Search ingestion
def encode_text_for_ai_search(text):
    return quote(text, safe="")

# Decode text to return it to its original format for user display
def decode_text_from_ai_search(encoded_text):
    return unquote(encoded_text)

DEBUG = os.environ.get("DEBUG", "false")
if DEBUG.lower() == "true":
    logging.basicConfig(level=logging.DEBUG)

AZURE_SEARCH_PERMITTED_GROUPS_COLUMN = os.environ.get(
    "AZURE_SEARCH_PERMITTED_GROUPS_COLUMN"
)


class JSONEncoder(json.JSONEncoder):
    def default(self, o):
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        return super().default(o)


async def format_as_ndjson(r):
    try:
        async for event in r:
            yield json.dumps(event, cls=JSONEncoder) + "\n"
    except Exception as error:
        logging.exception("Exception while generating response stream: %s", error)
        yield json.dumps({"error": str(error)})


def parse_multi_columns(columns: str) -> list:
    if "|" in columns:
        return columns.split("|")
    else:
        return columns.split(",")


def fetchUserGroups(userToken, nextLink=None):
    # Recursively fetch group membership
    if nextLink:
        endpoint = nextLink
    else:
        endpoint = "https://graph.microsoft.com/v1.0/me/transitiveMemberOf?$select=id"

    headers = {"Authorization": "bearer " + userToken}
    try:
        r = requests.get(endpoint, headers=headers)
        if r.status_code != 200:
            logging.error(f"Error fetching user groups: {r.status_code} {r.text}")
            return []

        r = r.json()
        if "@odata.nextLink" in r:
            nextLinkData = fetchUserGroups(userToken, r["@odata.nextLink"])
            r["value"].extend(nextLinkData)

        return r["value"]
    except Exception as e:
        logging.error(f"Exception in fetchUserGroups: {e}")
        return []


def generateFilterString(userToken):
    # Get list of groups user is a member of
    userGroups = fetchUserGroups(userToken)

    # Construct filter string
    if not userGroups:
        logging.debug("No user groups found")

    group_ids = ", ".join([obj["id"] for obj in userGroups])
    return f"{AZURE_SEARCH_PERMITTED_GROUPS_COLUMN}/any(g:search.in(g, '{group_ids}'))"


def format_non_streaming_response(chatCompletion, history_metadata, apim_request_id):
    response_obj = {
        "id": chatCompletion.id,
        "model": chatCompletion.model,
        "created": chatCompletion.created,
        "object": chatCompletion.object,
        "choices": [{"messages": []}],
        "history_metadata": history_metadata,
        "apim-request-id": apim_request_id,
    }

    if len(chatCompletion.choices) > 0:
        message = chatCompletion.choices[0].message
        if message:
            if hasattr(message, "context"):
                # Decode the context before adding it to the response
                decoded_context = decode_text_from_ai_search(json.dumps(message.context))
                response_obj["choices"][0]["messages"].append(
                    {
                        "role": "tool",
                        "content": decoded_context,
                    }
                )
            response_obj["choices"][0]["messages"].append(
                {
                    "role": "assistant",
                    "content": decode_text_from_ai_search(message.content),  # Decode AI Search results
                }
            )
            return response_obj
    return {}


def format_stream_response(chatCompletionChunk, history_metadata, apim_request_id):
    response_obj = {
        "id": chatCompletionChunk.id,
        "model": chatCompletionChunk.model,
        "created": chatCompletionChunk.created,
        "object": chatCompletionChunk.object,
        "choices": [{"messages": []}],
        "history_metadata": history_metadata,
        "apim-request-id": apim_request_id,
    }

    if len(chatCompletionChunk.choices) > 0:
        delta = chatCompletionChunk.choices[0].delta
        if delta:
            if hasattr(delta, "context"):
                # Decode context before adding to response
                decoded_context = decode_text_from_ai_search(json.dumps(delta.context))
                messageObj = {"role": "tool", "content": decoded_context}
                response_obj["choices"][0]["messages"].append(messageObj)
                return response_obj
            if delta.role == "assistant" and hasattr(delta, "context"):
                messageObj = {
                    "role": "assistant",
                    "context": decode_text_from_ai_search(delta.context),  # Decode context
                }
                response_obj["choices"][0]["messages"].append(messageObj)
                return response_obj
            else:
                if delta.content:
                    # Decode content before sending it
                    messageObj = {
                        "role": "assistant",
                        "content": decode_text_from_ai_search(delta.content),
                    }
                    response_obj["choices"][0]["messages"].append(messageObj)
                    return response_obj

    return {}



def format_pf_non_streaming_response(
    chatCompletion, history_metadata, response_field_name, citations_field_name, message_uuid=None
):
    if chatCompletion is None:
        logging.error(
            "chatCompletion object is None - Increase PROMPTFLOW_RESPONSE_TIMEOUT parameter"
        )
        return {
            "error": "No response received from promptflow endpoint. Increase PROMPTFLOW_RESPONSE_TIMEOUT parameter or check the promptflow endpoint."
        }
    if "error" in chatCompletion:
        logging.error(f"Error in promptflow response api: {chatCompletion['error']}")
        return {"error": chatCompletion["error"]}

    logging.debug(f"chatCompletion: {chatCompletion}")
    try:
        messages = []
        
        # Check and decode the main response field
        if response_field_name in chatCompletion:
            decoded_content = decode_text_from_ai_search(chatCompletion[response_field_name])
            messages.append({
                "role": "assistant",
                "content": decoded_content
            })
        
        # Check and decode the citations field
        if citations_field_name in chatCompletion:
            citation_content = {
                "citations": decode_text_from_ai_search(json.dumps(chatCompletion[citations_field_name]))
            }
            messages.append({
                "role": "tool",
                "content": json.dumps(citation_content)
            })

        response_obj = {
            "id": chatCompletion["id"],
            "model": "",
            "created": "",
            "object": "",
            "history_metadata": history_metadata,
            "choices": [
                {
                    "messages": messages,
                }
            ]
        }
        return response_obj
    except Exception as e:
        logging.error(f"Exception in format_pf_non_streaming_response: {e}")
        return {}



def convert_to_pf_format(input_json, request_field_name, response_field_name):
    output_json = []
    logging.debug(f"Input json: {input_json}")
    # Align the input json to the format expected by promptflow chat flow
    for message in input_json["messages"]:
        if message:
            if message["role"] == "user":
                encoded_content = encode_text_for_ai_search(message["content"])  # Encode the user message
                new_obj = {
                    "inputs": {request_field_name: encoded_content},  # Encode before sending to AI Search
                    "outputs": {response_field_name: ""},
                }
                output_json.append(new_obj)
            elif message["role"] == "assistant" and len(output_json) > 0:
                decoded_content = decode_text_from_ai_search(message["content"])  # Decode the AI response
                output_json[-1]["outputs"][response_field_name] = decoded_content
    logging.debug(f"PF formatted response: {output_json}")
    return output_json



def comma_separated_string_to_list(s: str) -> List[str]:
    '''
    Split comma-separated values into a list.
    '''
    return s.strip().replace(' ', '').split(',')

