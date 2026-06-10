# Apex Job Agent

An autonomous AI job search and application platform that discovers jobs, evaluates opportunities, generates tailored applications, and assists users throughout the job-hunting process.

## Overview

Apex Job Agent is an AI-powered job operations system designed to automate repetitive parts of the job search journey.

The platform combines job discovery, resume intelligence, application assistance, scoring systems, and browser automation into a single workflow.

## Features

### Job Discovery

* Job scraping from multiple sources
* LinkedIn integration
* Indeed integration
* Centralized job database

### AI Job Evaluation

* Job relevance scoring
* Skill matching
* Resume-job comparison
* Opportunity ranking

### Application Automation

* Form analysis
* Form filling
* Browser automation
* Application tracking

### Resume Intelligence

* Resume parsing
* Profile extraction
* Candidate profiling
* Skill identification

### AI Content Generation

* Personalized cover letters
* Application responses
* Recruiter outreach messages
* Job-specific tailoring

### Tracking Dashboard

* Applied jobs tracking
* Pipeline management
* Status updates
* Application history

### Multi-LLM Support

Supports multiple providers:

* OpenAI
* Anthropic Claude
* Gemini
* Mistral
* OpenRouter
* Ollama
* NVIDIA NIM

## Architecture

```text
apex_agent/
│
├── scraper/
│   ├── linkedin.py
│   └── indeed.py
│
├── intelligence/
│   ├── scorer.py
│   ├── ghostwriter.py
│   └── briefer.py
│
├── applier/
│   ├── form_analyzer.py
│   ├── form_filler.py
│   └── universal.py
│
├── tracking/
│   └── pipeline.py
│
├── db/
│   ├── jobs_db.py
│   └── profile_db.py
│
├── server/
│   ├── routes/
│   ├── websocket.py
│   └── app.py
│
└── main.py
```

## Tech Stack

### Backend

* Python
* FastAPI
* WebSockets

### Automation

* Playwright

### AI

* OpenAI
* Claude
* Gemini
* OpenRouter
* Ollama

### Storage

* SQLite

## Installation

### Clone Repository

```bash
git clone https://github.com/architaggarwal24/Apex-Job-Agent.git
cd Apex-Job-Agent
```

### Create Virtual Environment

```bash
python -m venv venv
```

Activate:

```bash
source venv/bin/activate
```

or on Windows:

```bash
venv\Scripts\activate
```

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Install Playwright Browsers

```bash
playwright install
```

### Configure Environment

```bash
cp .env.example .env
```

Add your preferred LLM API keys.

## Running the Application

### Start Backend

```bash
python main.py
```

or

```bash
uvicorn server.app:app --reload
```

Open:

```text
http://localhost:8000
```

## Workflow

1. Upload resume
2. Build candidate profile
3. Discover jobs
4. Score opportunities
5. Generate application content
6. Submit applications
7. Track progress

## Supported Capabilities

* Job search
* Resume parsing
* Application generation
* Cover letter writing
* Job scoring
* Pipeline tracking
* Browser automation

## Future Roadmap

* Multi-agent orchestration
* Resume versioning
* Interview preparation
* Recruiter CRM
* Automated follow-ups
* Analytics dashboard

## Author

Archit Aggarwal

## License

MIT License
