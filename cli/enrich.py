from __future__ import annotations

from core.orchestrator import OrchestratorOptions, run_enrich_pipeline


# Обертка для запуска пайплайна enrich с дефолтными опциями
def run_enrich() -> None:
    opts = OrchestratorOptions(
        # limit=100,
        # dry_run=False,
        # stop_on_error=False,
        # rate_limit_sec=0.0,
    )
    run_enrich_pipeline(opts)


# Локальный запуск из CLI: python -m cli.enrich
if __name__ == "__main__":
    run_enrich()
