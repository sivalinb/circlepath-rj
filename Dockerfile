FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY . .
RUN pip install --no-cache-dir --no-deps --no-build-isolation -e . \
    && useradd --create-home --uid 10001 circlepath \
    && mkdir -p /app/data && chown -R circlepath:circlepath /app
USER circlepath
ENV CIRCLEPATH_HOST=0.0.0.0 CIRCLEPATH_DATA_DIR=/app/data
EXPOSE 7860
CMD ["python", "-m", "circlepath", "serve"]
