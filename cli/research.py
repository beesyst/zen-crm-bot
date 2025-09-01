from __future__ import annotations

from core.orchestrator import OrchestratorOptions, run_research_pipeline


# Обертка для запуска пайплайна research с дефолтными опциями
def run_research() -> None:
    opts = OrchestratorOptions(
        # limit=100,
        # dry_run=False,
        # stop_on_error=False,
        # rate_limit_sec=0.0,
    )
    run_research_pipeline(opts)


# Локальный запуск из CLI: python -m cli.research
if __name__ == "__main__":
    run_research()
