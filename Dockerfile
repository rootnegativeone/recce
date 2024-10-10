# Use the official Playwright image with the required version
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file
COPY requirements.txt /app/

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . /app/

# Expose the port your app runs on
EXPOSE 5001

# Command to run on container start
CMD ["gunicorn", "--bind", "0.0.0.0:$PORT", "--timeout", "120", "app:app"]
