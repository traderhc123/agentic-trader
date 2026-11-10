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
#   docker run -d --name agentic-trader --restart on-failure -v agentic-trader-data:/data agentic-trader
#
# --restart on-failure, NOT unless-stopped/always: the agent exits with code 0
# when the owner stops it (dashboard Stop, `agent.py stop`) and non-zero when
# it crashes. on-failure restarts crashes and leaves a deliberate stop
# stopped; unless-stopped/always would start it trading again seconds after
# the owner pressed Stop. Start it again with: docker start agentic-trader
CMD ["python", "agent.py", "run"]
