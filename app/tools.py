"""
Tool/function definitions for the AI Voice Agent.

These tools are registered with Gemini Live and can be invoked
during voice conversations when the AI determines they're needed.

Tools:
- get_current_datetime: Returns current date, time, and day
- get_weather: Returns weather information for a location (mock data)
- search_knowledge_base: Searches the local knowledge base
"""

import aiohttp
from datetime import datetime
from pipecat.services.llm_service import FunctionCallParams

from app.config import settings
from app.knowledge import search_knowledge


# Tool Definitions (JSON Schema for Gemini)

TOOL_DEFINITIONS = [
    {
        "function_declarations": [
            {
                "name": "get_current_datetime",
                "description": "Get the current date, time, and day of the week. Use this when the user asks what time it is, what today's date is, or what day it is.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "timezone": {
                            "type": "string",
                            "description": "Optional timezone name (e.g., 'Asia/Kolkata', 'US/Eastern'). Defaults to server's local timezone.",
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "get_weather",
                "description": "Get the current weather for a specific location. Use this when the user asks about weather conditions, temperature, or forecast.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "The city or location to get weather for (e.g., 'Kochi', 'New York', 'London')",
                        }
                    },
                    "required": ["location"],
                },
            },
            {
                "name": "search_knowledge_base",
                "description": "Search the company knowledge base for information about TechNova Solutions, its services, pricing, policies, support, and FAQs. Use this FIRST when the user asks any question that might be related to the company.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query or question to look up in the knowledge base",
                        }
                    },
                    "required": ["query"],
                },
            },
        ]
    }
]


# Tool Handler Functions

async def get_current_datetime(params: FunctionCallParams):
    """
    Return the current date, time, and day of the week.

    This is called by Gemini when the user asks about the current time/date.
    """
    timezone_str = params.arguments.get("timezone", None)

    now = datetime.now()

    result = {
        "date": now.strftime("%B %d, %Y"),     
        "time": now.strftime("%I:%M %p"),        
        "day": now.strftime("%A"),            
        "timezone": timezone_str or "local timezone",
        "full": now.strftime("%A, %B %d, %Y at %I:%M %p"),
    }

    await params.result_callback(result)
    return result
async def get_weather(params: FunctionCallParams):
    """
    Fetch live weather data from Open-Meteo public API.
    If not able to fetch, return an error message to let the AI say sorry.
    """
    location = params.arguments.get("location", "").strip()
    if not location:
        result = {"error": "Sorry, I could not find that location."}
        await params.result_callback(result)
        return result

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Geocode location name to latitude and longitude
            geo_url = f"{settings.geocoding_api_url}/search?name={location}&count=1"
            async with session.get(geo_url, timeout=5) as geo_resp:
                if geo_resp.status != 200:
                    raise Exception("Geocoding service unavailable")
                geo_data = await geo_resp.json()

            results = geo_data.get("results")
            if not results:
                result = {"error": f"Sorry, I could not find the weather for {location}."}
                await params.result_callback(result)
                return result

            lat = results[0]["latitude"]
            lon = results[0]["longitude"]
            resolved_name = results[0].get("name", location)

            # 2. Fetch current weather
            weather_url = f"{settings.weather_api_url}/forecast?latitude={lat}&longitude={lon}&current_weather=true"
            async with session.get(weather_url, timeout=5) as weather_resp:
                if weather_resp.status != 200:
                    raise Exception("Weather service unavailable")
                weather_data = await weather_resp.json()

            current = weather_data.get("current_weather")
            if not current:
                raise Exception("No weather data returned")

            temp = current.get("temperature")
            wind = current.get("windspeed")
            code = current.get("weathercode", 0)

            # Map weather code to human description
            WEATHER_CODES = {
                0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
                45: "Foggy", 48: "Depositing rime fog", 51: "Light drizzle",
                53: "Moderate drizzle", 55: "Dense drizzle", 61: "Slight rain",
                63: "Moderate rain", 65: "Heavy rain", 71: "Slight snow fall",
                73: "Moderate snow fall", 75: "Heavy snow fall", 80: "Slight rain showers",
                81: "Moderate rain showers", 82: "Violent rain showers", 95: "Thunderstorm",
                96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail"
            }
            condition = WEATHER_CODES.get(code, "Unknown weather condition")

            result = {
                "location": resolved_name,
                "temperature": f"{temp}°C",
                "condition": condition,
                "wind_speed": f"{wind} km/h",
            }

    except Exception as e:
        result = {"error": f"Sorry, I was unable to fetch the live weather for {location} at this moment."}

    await params.result_callback(result)
    return result

async def search_knowledge_base_tool(params: FunctionCallParams):
    """
    Search the local knowledge base and return relevant information.

    This is called by Gemini when the user asks questions that might
    be answered by the knowledge base.
    """
    query = params.arguments.get("query", "")

    results = search_knowledge(query)

    if results:
        result = {
            "found": True,
            "answers": results,
            "source": "company knowledge base",
        }
    else:
        result = {
            "found": False,
            "message": "No relevant information found in the knowledge base. Please use your general knowledge to answer.",
        }

    await params.result_callback(result)
    return result
