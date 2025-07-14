# Use official Python 3.10 slim image
FROM python:3.10-slim

# Set working directory
WORKDIR /app
ADD . /app

# Copy requirements and bot code
COPY requirements.txt .
COPY bot.py .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Run the bot
CMD ["python", "bot.py"]
