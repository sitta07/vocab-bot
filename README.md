# AI Vocab Coach (MLOps Edition)

**A Serverless AI-Driven English Learning Assistant on LINE.**

## Project Overview

AI Vocab Coach is a serverless application integrated with the LINE Messaging API, designed to facilitate English vocabulary acquisition through active recall and spaced repetition. It leverages **Google Gemini 1.5 Flash** for natural language processing tasks, including translation, context-aware example generation, and grammar correction.

The system is built on a serverless architecture to ensure scalability and zero maintenance costs. It demonstrates practical **MLOps** implementation by collecting interaction logs (user answers vs. AI grading) for future model analysis and fine-tuning.

## Key Features

* **Smart Dictionary:** Users can add new words, and the system automatically provides translations and example sentences using Generative AI.
* **Automated Spaced Repetition:** The system utilizes GitHub Actions as a cron scheduler to trigger vocabulary quizzes three times a day (Morning, Noon, Evening), ensuring consistent practice without requiring a dedicated 24/7 server.
* **AI Grammar Coach:** Users can respond to quizzes with English sentences. The AI evaluates the grammar and context, ignores minor punctuation errors, and provides constructive feedback.
* **Data Persistence & Logging:** All vocabulary data and user interaction logs are stored in a PostgreSQL database (Supabase) for progress tracking and MLOps data collection.

## Technical Architecture

The project follows a microservices-based, serverless approach:

1.  **Interface:** LINE Messaging API
2.  **Backend:** Python (FastAPI) running on Render (Dockerized)
3.  **AI Model:** Google Gemini 1.5 Flash
4.  **Database:** Supabase (PostgreSQL)
5.  **Automation:** GitHub Actions (Scheduled Triggers)

## How It Works

1.  **Input:** A user sends a word to the LINE bot.
2.  **Processing:** The backend sends the text to Google Gemini to generate metadata (meaning, translation, example).
3.  **Storage:** The data is saved to Supabase.
4.  **Active Recall:** At scheduled intervals, GitHub Actions triggers the backend to retrieve a random word and push a quiz to the user.
5.  **Feedback Loop:** The user responds to the quiz. The AI evaluates the answer, provides feedback, and logs the performance result to the database.