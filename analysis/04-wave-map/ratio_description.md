For a wave w, a region R and a period t (one year since take-off, or everything up to a given month):

  ratio    = actual / expected

  actual   = number of w's new companies in period t that are located in R

  expected = sum, over every new company i in w during period t, of  s_R(year of i)

  s_R(y)   = among companies with at least one product release in calendar
             year y that does NOT match w's technology family, the share located in R

  Each new company in the wave adds a fraction to the expected count: the chance that a typical company releasing products in its calendar year is located in R. Product releases are those the launch classifier flags, in English, from senders that are placed and are not market-research firms, law firms, research publishers or stock-commentary senders. Each company counts once per year, however many releases it sends.

  When all of a period's companies entered in the same calendar year, this simplifies to two shares:

  ratio = (share of the wave's new companies located in R)
          / (share of companies with other product releases located in R, same year)

  Example (illustrative numbers). Suppose generative AI gains 100 new companies in 2023, and 12 of them are in Silicon Valley. Suppose also that 4.4% of the companies with a 2023 product release outside generative AI's family are in Silicon Valley.

  expected = 100 × 0.044 = 4.4
  ratio    = 12 / 4.4   ≈ 2.7×

  Silicon Valley holds 2.7 times as many of that year's generative AI newcomers as its usual share would predict.

Take-off is the first month in which the wave's share of press releases reaches half of its peak. It's the half_from column in 03-tech-waves/waves.txt, computed by 03-tech-waves/plot.R.

  rate(m)  = 1000 × (releases matching the wave's terms in the 12 months centred on m)
                   / (all releases in the same 12 months)

  peak     = the highest rate(m) between 2011-01 and the last complete month

  take-off = the first month m with rate(m) >= peak / 2

  Details
  - Which releases count: English releases from senders that aren't market-research firms. The denominator uses the same filter.
  - The rate: matching releases per 1,000, smoothed over a centred 12-month window. The window is a ratio of sums, so thin months weigh less.
  - Grouping: take-off uses all releases, not just product launches.

  In plain terms, take-off is when the wave reaches half its eventual prominence in the newswire. It marks the start of a wave's main phase, not its first appearance.

  Caveats
  - Mobile apps (2011-06) and cloud (2011-12): both were already above half their peak when the data starts. Their take-off dates mark the start of the data, so their real take-off was earlier.
  - Any AI (2023-12) and agentic AI (2025-05): they peak in the most recent months (2026-03). If they keep rising, the peak will move up and take-off will move later.
