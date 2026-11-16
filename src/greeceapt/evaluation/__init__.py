"""Vision scoring evaluation harness — golden set, pairwise labels, Bradley-Terry, bake-off.

The visual score is 20% of the deal score but had collapsed to ~12 distinct values (46% were
exactly 5.0). This package makes it a *measured* signal: you rank apartment photos pairwise,
Bradley-Terry turns that into a latent quality scale, and candidate scorers are graded by how
well they agree with your ranking — with a confidence interval, on a locked holdout.
"""
