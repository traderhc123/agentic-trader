FROM python:3.12-slim

# Non-root user; agent state (consent, config, tokens, trade log) lives in
# the /data volume via AGENT_HOME.
RUN useradd --create-home --uid 1000 trader
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir /data && chown trader:trader /data
USER trader
ENV AGENT_HOME=/data

# First run (interactive consent + setup):
#   docker run -it -v agentic-trader-data:/data agentic-trader python agent.py setup
# Then the heartbeat:
#   docker run -d --restart unless-stopped -v agentic-trader-data:/data agentic-trader
CMD ["python", "agent.py", "run"]
