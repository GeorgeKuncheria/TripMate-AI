# TEST WITHOUT MCP CLIENT

# from tools.tavily_tool import tavily_search
# from tools.flight_tool import search_flights
# from backend import run_travel_agent

# # res = tavily_search("Best hotels in India")
# # print(res)


# # res = search_flights("Plan a 7 days Japan trip from delhi")
# # print(res)

# user_input = input("Enter travel request: ")

# response = run_travel_agent(
#     user_input=user_input,
#     thread_id="test_user"
# )

# print("\nFINAL RESPONSE:\n")
# print(response["answer"])


# ------------------------------------------------------------------------------

# TEST WITH MCP CLIENT

import asyncio
# from mcp_client_test import get_all_tools, tavily_mcp_search
# from mcp_client_test import get_all_tools, tavily_mcp_search
from mcp_client import initialize_mcp, get_all_tools



if __name__ == "__main__":
    # query = "Latest news about AI in 2026"
    # asyncio.run(tavily_mcp_search(query))

    asyncio.run(get_all_tools())