# Container image for the API. Used by Hugging Face Spaces (Docker SDK) and runs anywhere else.
# Portal credentials come from the environment (URJA_EMAIL, URJA_PASSWORD), never the image.
FROM python:3.11-slim

# Hugging Face Spaces run containers as uid 1000.
RUN useradd --create-home --uid 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH" \
    PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user app ./app

EXPOSE 7860
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860} --proxy-headers --forwarded-allow-ips='*'"]
