"""
Rental listings → calibrated rent model → euro rent and yield for sale listings.

    python -m greeceapt.rent scrape      # Spitogatos rentals → data/rentals.db
    python -m greeceapt.rent sample      # pick the stratified vision sample
    python -m greeceapt.rent vision      # Qwen V2 condition score on the sample (resumable)
    python -m greeceapt.rent train       # fit + spatial CV → data/rent_model.json + report
"""
