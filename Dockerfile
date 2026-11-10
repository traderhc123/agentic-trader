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
#
# Stop it with:  docker exec agentic-trader python agent.py stop
# `docker stop` works too: the agent treats SIGTERM as a stop request,
# finishes what it is doing and exits 0. But Docker ends it by force after
# 10 s (exit code 137), and on-failure reads that as a crash at the next
# daemon/host restart — use `docker stop -t 150`. A clean host or daemon
# restart also ends it with exit 0, so it is NOT started again afterwards:
# run `docker start agentic-trader`. See GETTING_STARTED.md.
CMD ["python", "agent.py", "run"]
