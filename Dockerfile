# Use a slim Python image to keep size manageable
FROM python:3.10-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7860

# Set working directory inside the container
WORKDIR /app

# Install system dependencies required by OpenCV, EasyOCR, and audio components
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsm6 \
    libxext6 \
    libgl1-mesa-glx \
    libglib2.0-0 \
    build-essential \
    && rm -rf /lib/apt/lists/*

# Copy requirements file first to utilize Docker build cache
COPY requirements.txt /app/

# Install dependencies (using CPU-only PyTorch to fit constraints and build faster if needed)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files
COPY . /app/

# Create a local upload directory with write permissions
RUN mkdir -p /app/static/uploads && chmod -R 777 /app/static/uploads

# Expose port 7860 (Hugging Face Spaces requirement)
EXPOSE 7860

# Run the Flask app with Gunicorn, binding to port 7860
CMD ["gunicorn", "-b", "0.0.0.0:7860", "--timeout", "120", "app:app"]
