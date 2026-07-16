"""
Pipecat pipeline setup for the AI Voice Agent.

Configures the voice agent pipeline with:
- Twilio WebSocket transport (audio I/O)
- Gemini Live LLM service (speech-to-speech AI)
- Tool/function registration
- Barge-in support
- Event logging
"""

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.google.gemini_live import GeminiLiveLLMService
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport, FastAPIWebsocketParams
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMRunFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.services.llm_service import FunctionCallParams

from app.config import settings
from app.prompts import SYSTEM_PROMPT, GREETING
from app.tools import get_current_datetime, get_weather, search_knowledge_base_tool
from app.tools import TOOL_DEFINITIONS
from app.utils.logger import (
    logger,
    log_call_connected,
    log_call_ended,
    log_pipeline_event,
    log_tool_invoked,
    log_tool_result,
)


async def run_bot(websocket, stream_sid: str = None, call_sid: str = None):
    """
    Set up and run the Pipecat voice agent pipeline.

    This function creates the complete pipeline:
    Twilio Audio In → Gemini Live → Twilio Audio Out

    Args:
        websocket: The WebSocket connection from Twilio
        stream_sid: Twilio Media Stream SID
        call_sid: Twilio Call SID
    """

    log_pipeline_event("INITIALIZING", f"Stream SID: {stream_sid}")

    # 1. Configure Twilio Transport 
    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            serializer=serializer,
        ),
    )

    # 2. Configure Gemini Live LLM Service
    llm = GeminiLiveLLMService(
        api_key=settings.gemini_api_key,
        settings=GeminiLiveLLMService.Settings(
            model="models/gemini-2.5-flash-native-audio-preview-12-2025",
            voice="Puck",
            system_instruction=SYSTEM_PROMPT,
            thinking={"thinking_budget": 0},
        ),
        tools=TOOL_DEFINITIONS,
    )

    # 3. Register Tool Handlers 

    async def handle_get_datetime(params: FunctionCallParams):
        """Handle the get_current_datetime tool call."""
        log_tool_invoked("get_current_datetime", params.arguments)
        result = await get_current_datetime(params)
        log_tool_result("get_current_datetime", result if isinstance(result, dict) else {"status": "completed"})

    async def handle_get_weather(params: FunctionCallParams):
        """Handle the get_weather tool call."""
        log_tool_invoked("get_weather", params.arguments)
        result = await get_weather(params)
        log_tool_result("get_weather", result if isinstance(result, dict) else {"status": "completed"})

    async def handle_search_kb(params: FunctionCallParams):
        """Handle the search_knowledge_base tool call."""
        log_tool_invoked("search_knowledge_base", params.arguments)
        result = await search_knowledge_base_tool(params)
        log_tool_result("search_knowledge_base", result if isinstance(result, dict) else {"status": "completed"})

    llm.register_function("get_current_datetime", handle_get_datetime)
    llm.register_function("get_weather", handle_get_weather)
    llm.register_function("search_knowledge_base", handle_search_kb)

    # 4. Setup Conversation Context & VAD
    messages = [
        {
            "role": "user",
            "content": "Start the conversation: welcome the caller to TechNova Solutions, introduce yourself, and offer to assist them in English or Malayalam.",
        }
    ]

    context = LLMContext(messages)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.25)),
        ),
    )

    # 5. Build the Pipeline
    pipeline = Pipeline(
        [
            transport.input(),      # Twilio audio input
            user_aggregator,        # Handles incoming voice activity detection and audio chunking
            llm,                    # Gemini Live (speech-to-speech)
            transport.output(),     # Twilio audio output
            assistant_aggregator,   # Updates conversation context with AI responses
        ]
    )

    # 6. Create the Pipeline Task 
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
        ),
    )

    # 7. Event Handlers 
    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport_instance, client):
        log_call_connected(call_sid or "unknown")
        log_pipeline_event("CLIENT CONNECTED", "Audio streaming started")
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport_instance, client):
        log_call_ended(call_sid or "unknown")
        log_pipeline_event("CLIENT DISCONNECTED", "Audio streaming ended")
        await task.cancel()

    # 8. Run the Pipeline
    log_pipeline_event("STARTING", "Pipeline is now running")

    runner = PipelineRunner()
    await runner.run(task)

    log_pipeline_event("STOPPED", "Pipeline has stopped")
