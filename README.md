# Xenora AI 🌌

**Xenora** is an autonomous, vision-enabled AI desktop companion. It acts as a smart operating system assistant, combining cloud and local AI models to manage files, automate desktop workflows, and converse naturally.

##  Core Features

* **Hybrid AI Engine:** Intelligently routes tasks through Groq (fast online), Google Gemini (vision/fallback), and local Ollama (`deepseek-r1:8b`) for offline capability.
* **Desktop & Browser Control:** Drives your PC using PyAutoGUI and PyWinAuto for desktop apps, and Playwright for seamless browser automation.
* **Real-Time Screen Awareness:** Periodically captures screen state to offer contextual help, analyze images, and answer visual questions.
* **File & Workspace Management:** Searches drives, organizes workspaces, and natively reads, writes, or edits documents (PDF, Word, Excel, Markdown).
* **Autonomous Planning:** Breaks down multi-step instructions, manages recurring routines, and tracks app presence/triggers.
* **Local Voice:** Speaks responses aloud with low-latency using the offline Kokoro ONNX text-to-speech engine.

##  Getting Started

### 1. Prerequisites
* Python 3.10+
* [Ollama](https://ollama.com/) installed locally (for offline fallbacks).
* `ffmpeg` installed and added to your system PATH (required for media downloads).

### 2. Installation
Clone the repository and install the dependencies:
```bash
git clone [https://github.com/Kunalvashist7744/Xenora-AI.git](https://github.com/Kunalvashist7744/Xenora-AI.git)
cd Xenora-AI
pip install -r requirements.txt
```

### 3. Configuration
Create a .env file in the root directory with your keys:
```bash
GROQ_API_KEY=your_groq_api_key_here
GEMINI_API_KEY=your_gemini_api_key_here
NVIDIA_API_KEY=your_nvidia_api_key_here
TAVILY_API_KEY=your_tavily_api_key_here
XENORA_OWNER_NAME=Kunal
```

## License

Copyright (c) 2026 Kunal Vashist. All rights reserved.
Unauthorized copying of this file, via any medium, is strictly prohibited. Proprietary and confidential.
