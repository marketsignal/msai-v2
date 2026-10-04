# Research selection boundary — October 4, 2026

Inspected installed Optuna is4.8.0 in the available local interpreter; current official stable documentation rendered5.0.0, so do not treat that as an installed upgrade. No dependency change is proposed.

Official [scikit-learn leakage guidance](https://scikit-learn.org/stable/common_pitfalls.html#data-leakage) states that test information must not choose the model; applying that principle to this repository supports freezing a training choice before reserved-period diagnostics. This is a design inference, not evidence the platform uses scikit-learn or produces alpha.

Official [Optuna ask/tell](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/009_ask_and_tell.html) describes passing objective feedback back to the study. [create_study](https://optuna.readthedocs.io/en/stable/reference/generated/optuna.create_study.html) documents `load_if_exists`: reuse loads the existing named study. Consequently the repair must both send training scores and prevent old holdout-driven studies from influencing new runs. A fresh per-invocation namespace is the smallest safe boundary while immutable strategy/data/cost identity remains open; it suspends cross-job reuse explicitly.

Existing project ownership map `docs/audits/2026-10-03/next-research-scope.md` and current engine/worker/API/UI inspection cover local decisions. No new quantitative-validation library, nested-CV subsystem, database or scheduler is needed for this bounded repair. Full immutable experiment and independent final-validation evidence remain a later accepted milestone dependency.
