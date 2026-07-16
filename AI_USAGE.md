# AI Usage Documentation

This document details how AI-assisted development tools were used throughout the development of this AI Voice Agent project.

## AI Tools Used

### 1. Gemini (Google AI Studio)
- **Purpose**: Code generation, architecture design, debugging
- **How Used**: 
  - Designed the overall system architecture
  - Generated boilerplate code for FastAPI endpoints
  - Created the Pipecat pipeline configuration
  - Wrote tool/function definitions for Gemini Live
  - Generated the knowledge base data
  - Helped with Twilio integration patterns

### 2. Antigravity IDE (AI Coding Assistant)
- **Purpose**: Full-stack development assistance
- **How Used**:
  - Deep analysis of the project requirements document
  - Created comprehensive project understanding document
  - Generated implementation plan
  - Built all project files with proper structure
  - Researched Pipecat, Gemini Live, and Twilio documentation
  - Implemented best practices for audio streaming architecture

## What Code Was Modified Manually

- `.env` file: Updated with actual API credentials (security-sensitive)
- System prompt in `prompts.py`: Fine-tuned after testing voice conversations
- Knowledge base in `knowledge.json`: Adjusted FAQ entries based on testing
- `bot.py`: Adjusted Pipecat pipeline configuration for audio quality tuning

## Challenges Encountered and Solutions

### 1. Pipecat + Gemini Live Integration
- **Challenge**: Understanding the correct way to use `GeminiLiveLLMService` with speech-to-speech mode
- **Solution**: Researched Pipecat documentation and GitHub examples to find the correct pipeline configuration for Twilio transport with Gemini Live

### 2. Audio Transcoding
- **Challenge**: Twilio uses G.711 µ-law (8kHz) while Gemini expects PCM (16kHz)
- **Solution**: Pipecat's `TwilioTransport` handles this automatically — no manual transcoding needed

### 3. Tool Calling in Speech-to-Speech Mode
- **Challenge**: Figuring out how to register and handle function calls with Gemini Live
- **Solution**: Used Pipecat's `@llm.function()` decorator pattern with `FunctionCallParams` for async tool execution

### 4. Malayalam Language Support
- **Challenge**: Ensuring Gemini Live can handle Malayalam speech input/output
- **Solution**: Configured the system prompt to instruct bilingual behavior and tested with native Malayalam speakers

### 5. Barge-in Handling
- **Challenge**: Making the AI stop speaking when the user interrupts
- **Solution**: Enabled `allow_interruptions=True` in `PipelineParams` — Pipecat + Gemini Live handle this natively

## Development Approach

1. **Requirements Analysis**: Thoroughly analyzed the project document to understand all requirements
2. **Architecture Design**: Designed the data flow: Phone ↔ Twilio ↔ Pipecat/FastAPI ↔ Gemini Live
3. **Foundation First**: Set up project structure, configuration, and logging before core logic
4. **Iterative Development**: Built and tested each component incrementally
5. **AI-Assisted Coding**: Used AI tools for boilerplate generation while manually reviewing and modifying critical logic

## Time Breakdown

| Phase | Time Spent | Description |
|-------|-----------|-------------|
| Analysis & Planning | ~30 min | Requirements analysis, architecture design |
| Foundation Setup | ~20 min | Project structure, config, dependencies |
| Core Pipeline | ~60 min | FastAPI, Pipecat pipeline, Twilio integration |
| Features | ~45 min | Tools, knowledge base, prompts |
| Testing & Debugging | ~45 min | End-to-end testing, bug fixes |
| Documentation | ~20 min | README, AI_USAGE.md, understanding.md |
| **Total** | **~3.5 hours** | |
