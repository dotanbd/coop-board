# Use the official Python image
FROM python:3.10-slim

# Set the working directory inside the container
WORKDIR /app

# Prevent Python from writing pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the backend code into the container
COPY . .

# Expose the port Uvicorn runs on
EXPOSE 8001

# Start the FastAPI server (adjust the filename if your main file isn't main.py)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]