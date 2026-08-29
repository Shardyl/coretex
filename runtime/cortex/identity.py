"""One definition of 'this is US' — every module that needs the company domains imports from here.
The audit found three drifting copies (engine.OWN_COMPANY_DOMAINS, meetnotes._OWN, scrape rules)."""
OWN_COMPANY_DOMAINS = {"sensa.digital", "skyvision.film", "tabscanner.com", "snap-rewards.com",
                       "filmspoke.ai", "coretex.uk", "sensa.film", "sensafilms.com"}
# domains that attend our meetings but are not client organisations (the owner's personal account)
NON_CLIENT_DOMAINS = OWN_COMPANY_DOMAINS | {"gmail.com"}
