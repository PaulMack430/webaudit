# AI Integration

WebAudit uses the Claude API (`claude-sonnet-4-6`) to generate two types of outreach content per lead: a cold call script and a follow-up email. Both are grounded in the specific audit findings for that business — not generic templates.

## Why Claude, and why here

The audit step produces a structured list of website issues. Turning that list into personalized, natural-sounding outreach is a judgment-based task where the right output depends on context: the business category, the severity of the issues, and the right tone for the audience. This is exactly where an LLM outperforms a template — the same set of issues should read differently for a law firm vs. a food truck.

Deterministic code handles everything with a right answer (SSL check, viewport tag presence). Claude handles everything that requires judgment (how to frame those findings as a compelling pitch to a specific business owner).

## Prompt design

### Cold call script — system prompt

```
You are a web consultant helping a freelancer prepare for a cold call.
Write a short, natural cold call script (under 200 words) for the business below.

Structure:
1. Friendly opener with business name
2. One-sentence hook referencing a specific website problem
3. Brief value proposition (what fixing it means for their business)
4. Soft close: ask for a 10-minute call this week

Rules:
- Never use jargon (no "SSL certificate", "meta tags", "viewport"). Translate to business impact.
- Write in a conversational tone, not a sales pitch tone.
- Reference only the most impactful 1-2 issues, not every finding.
- Do not mention prices.
```

### Cold call script — user message

```
Business: {name} ({category})
Website: {website}

Website issues found:
{formatted_errors}
```

Where `formatted_errors` translates technical findings into impact statements:
- `missing_viewport` → "Your website is hard to use on mobile phones"
- `no_ssl` → "Your website shows a 'Not Secure' warning to visitors"
- `broken_links: 3` → "3 links on your site lead to dead pages"

### Follow-up email — differences

The email prompt uses the same business + audit data but with different output constraints:

- Subject line required as first line, prefixed `Subject:`
- 3–4 short paragraphs
- Plain text only (no bullet points, no formatting)
- Tone: warm but direct, not salesy

### Validation

Both endpoints validate the response before returning it:

**Script:** must be 50–300 words, must contain the business name.

**Email:** must begin with `Subject:`, must contain at least 3 paragraph breaks.

On failure, a single retry fires with an appended instruction: `"Please follow the format exactly. Previous response did not meet the requirements."`

## Iteration notes

**v1 (generic):** passed a flat list of error codes → output was generic ("your website has some issues that may be affecting your SEO"). Essentially a template with the business name filled in.

**v2 (impact framing):** translated error codes to impact statements before passing to the model → output became specific and actionable. Conversion-relevant.

**v3 (scope limiting):** added instruction to reference only the top 1–2 issues → shorter, punchier scripts. Earlier versions tried to cover every finding and sounded like a laundry list.

The key insight: the model performs better when the *input* is already framed correctly. Pre-processing the audit data into business-impact language is at least as important as the prompt itself.

## Future directions

- Use tool use / function calling to let Claude query the audit data itself rather than packing everything into the prompt
- Explore using Claude as a browser agent for the discovery layer — navigating directory sites dynamically rather than scraping fixed CSS selectors
- A/B testing prompt variants against call outcome data to measure which script structures convert better
