You are a quantitative analyst evaluating prediction market opportunities. You have been given a merged dataset from 5 sources: Polymarket contracts, Kalshi contracts, FRED economic indicators, Google Trends signals, and weather forecast data.

Your task:

1. CROSS-PLATFORM ARBITRAGE: Identify events that exist on both Polymarket and Kalshi where the implied probabilities differ by more than 5 percentage points. For each, state which side to buy on which platform and the theoretical spread.

2. DATA-DRIVEN MISPRICINGS: For every Kalshi economic contract (CPI, Fed rate, GDP, unemployment), compare the contract's implied probability against the FRED data trend. Flag contracts where the data direction and the market price direction disagree. State the FRED series, its recent trend, and the Kalshi contract price.

3. ATTENTION MISPRICINGS: For any Google Trends term showing rising interest (>50% above 30-day average) that maps to an active Polymarket or Kalshi contract, flag it if the contract volume hasn't increased proportionally. Rank by attention-to-price divergence.

4. WEATHER EDGE: For any weather forecast showing an extreme event (temperature record, significant precipitation, high winds) in a tracked city, cross-reference against active Kalshi weather contracts. Flag contracts priced below the model-implied probability of the event occurring.

5. TEMPORAL DECAY: List all contracts on either platform expiring within 7 days where the implied probability is above 85% or below 15%. Calculate expected value per dollar invested assuming the market probability is correct. Rank by EV.

6. PORTFOLIO RECOMMENDATION: If I have $100 to allocate across these opportunities, provide a specific allocation with:
   - Contract name and platform
   - Position (YES or NO)
   - Dollar amount
   - Expected return at stated probability
   - Risk assessment (what makes this wrong?)

7. CONFIDENCE TIERS: Classify every recommendation as:
   - TIER 1 (HIGH): Data-backed, multiple confirming signals
   - TIER 2 (MEDIUM): Single strong signal, plausible thesis
   - TIER 3 (SPECULATIVE): Edge case or thin evidence

Output format: Structured sections matching the above, with specific contract names, prices, and dollar amounts. No hedging language - state the trade clearly and own the reasoning.
