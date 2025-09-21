# 1. Base Image: Use an official, lightweight Python runtime
FROM python:3.11-slim

# 2. Set Environment: Prevents Python from writing .pyc files
ENV PYTHONDONTWRITEBYTECODE 1
# Ensures Python output is sent straight to the terminal without buffering
ENV PYTHONUNBUFFERED 1

# 3. Set the working directory inside the container
WORKDIR /app

# 4. Install Dependencies: Copy only the requirements file first to leverage Docker's build cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy Application Code: Copy your main script and credentials file.
COPY main.py .
# COPY firebase-credentials.json .

# 6. Run Command: Specify the command to run when the container starts.
CMD ["python", "main.py"]