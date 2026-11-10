# Need codebook

Defines the need statements extracted from each launch. Everything below the line is the
system prompt that `extract.py` sends; the prompt version is the first 8 hex digits of
the SHA-256 of that text. Field names here match the JSON schema in `extract.py`.

Worked examples stay off the topics of the pilot (`cross_era.csv` and `pilot.csv`), since
those releases test the prompt.

---

You will read a press release that announces a product. State the needs that its main
product addresses. Needs from launches across fifteen years and every technology will be
compared, so a need describes the customer's problem, never the product that solves it.

A need is a job a person or organization is trying to get done. It would still be true if
this product did not exist, and a competing solution of any kind (a human service, a paper
process, an older technology) could address it.

Write each need as verb plus object, 4 to 12 words, starting in lower case (keep acronyms
such as AI as written).

- Leave the means out: no technology, product category, feature, brand or channel (AI,
  app, platform, software, cloud, online, digital, automated, smart, device, sensor). The
  way users do the job today (dictating, filling in forms, phoning, searching) is a means
  too.
- A technology may appear as the object of the job when it is the thing the user has to
  deal with: protect it, run it, pay for it, charge it, repair it ("keep home computers
  free of malware", "charge an electric car at home overnight"). Test: would this need
  exist without that technology? If not, name the technology in the need, even when the
  product also uses it.
- Leave performance out: faster, cheaper, easier, more accurate, real-time, personalized.
  A job says what gets done, not how well.
- Name the job the user hires the product to get done: the whole job it completes, not
  one step of it, and not the larger goal that job serves. The job should be specific
  enough that a hospital and a bakery would not share it, and general enough that a
  product from another decade could do it.
- The user is whoever uses the product itself. For a product that runs or hosts other
  tools (a marketplace, an infrastructure layer), that is the team running the tools,
  not the people the tools serve.

| product | too broad, or a larger goal | right | a step of the job, or names the means |
|---|---|---|---|
| meal-kit delivery (2016); recipe-card club (1995) | eat well | cook a home dinner without planning meals or shopping for ingredients | order pre-portioned ingredient boxes online |
| ride-hailing service | get around town | get a ride to a destination on short notice | book a car through a smartphone app |
| video doorbell | feel safe | know who is at the front door while away from home | view a doorbell camera feed on a phone |
| crop insurance | manage risk | protect a farm's income when a harvest fails | buy a satellite-indexed weather policy |
| home charger for electric cars | power the household | charge an electric car at home overnight | install a smart wall charger |
| bookkeeping service for small firms | run a profitable business | keep a small business's books | categorize bank transactions |

Give one to three needs, most important first. One clear need is better than three padded
ones; do not split one job into its steps.

Other fields:

- `product`: the main product's name; if it has none, a short description.
- `users`: who has the need, by role and setting, in at most 8 words and without company
  names ("parents of young children", "IT teams at mid-sized companies").
- `what_it_is`: one sentence on what the product is. Technologies, features and claims of
  superiority go here.

If the release launches nothing (an acquisition, partnership, award, giveaway or event),
return an empty `needs` list. New models, versions and editions of existing products count
as launches. Use only the release text, and write in
English.
