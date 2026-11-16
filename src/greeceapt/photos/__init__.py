"""AI pipeline layers (metadata filter → Qwen condition audit → quality gate).

Import submodules explicitly, e.g. ``greeceapt.photos.ai_conductor``.
Avoid re-exporting ``run`` here so ``python -m greeceapt.photos.ai_conductor``
does not hit a half-initialized package.
"""
