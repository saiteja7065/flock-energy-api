# Reflection

Honest answers to the five questions. The technical detail behind most of this is in
[PROTOCOL.md](PROTOCOL.md); this is more about how it went.

---

## 1. What assumptions did I make?

The biggest one is about the **network hierarchy**.

When I first lined up the data, it looked broken. The same division code (`D-01`) showed up
under three different circles, and feeders like `F-001` appeared under two substations. I had
to decide what that meant. It could be one division shared by three circles, but that doesn't
make sense for how distribution networks are built: power flows down a tree, and a feeder
doesn't hang off two substations at once. So I assumed the codes are only unique *within
their parent*, like house numbers on different streets. I identify each node by its full
path (`Z-01/C-01/D-01`) instead of the code alone.

Other assumptions:

- **The majority is right.** Each transformer has about 10 meters. When one meter has a blank
  circle code and the other nine agree, I treat the blank as a data-entry slip and fill it in.
  Every fix is recorded on the meter, so nothing is changed silently.
- **The "Old Malviya Nagar Xfmr" meters belong to DT-007.** Three meters carry that old name,
  but their codes all say DT-007. They're also the last three IDs and the only ones with a
  `GN` serial format, so they look like later additions. I'm fairly confident, but I can't
  prove it.
- **Timestamps are Indian time (IST).** The portal doesn't say, but the utility is in Jaipur.
- **`kwh` is a running meter total, not usage per reading.** It only ever goes up, by small
  steady amounts, so actual consumption is the difference between two readings.
- **The data doesn't change by the minute,** so serving a snapshot up to 15 minutes old is fine.

---

## 2. What was the hardest part, and how did I get unstuck?

**Getting past the HTML.**

The meters page loaded with an empty table, literally "No meters found", even though I was
logged in. That confused me for a bit, until I realised the data must be filled in afterwards
by JavaScript. So instead of scraping the page, I read the JavaScript files the browser
downloads and searched them for `fetch(` calls. That one search gave me almost the whole map:
the search API, the energy and location endpoints, and an **Export** button that downloads
every meter in one go.

The export was protected by a request signature, which felt like a dead end at first. But the
signing code was right there in the browser JavaScript, about ten lines of HMAC-SHA256, so I
could copy it exactly in Python. It worked on the first real try, which was a good moment.

The second hurdle was the meter detail page. Its data comes in SvelteKit's own compact format
(a flat list where values point to each other by position), so I had to write a small decoder.
Then I noticed some meters came back in a completely different layout. Luckily the portal's
own code had an `if ("classData" in …)` branch for exactly that case, which told me it was
intentional and not random corruption.

---

## 3. What would I improve with another day?

- **Pull consumption for every meter in the background.** Right now consumption is fetched one
  meter at a time. With all of it collected (about 4 minutes, staying under the rate limit) I
  could answer the questions that actually matter to a utility: Which transformers are close
  to overload? Which meters have flat or backwards readings? Where are voltages drifting?
- **Build a small web dashboard:** a map of meters coloured by status, a clickable hierarchy
  tree, and consumption charts.
- **Add scheduled checks against the live portal.** Services like this usually break because
  the *portal* changes, not because of a bug in our code. An automated check would catch that early.

---

## 4. What mistake did I make?

**I found the rate limit by running straight into it.**

To check the energy data for problems, I wrote a script that requested all 403 meters back to
back. About 236 succeeded, then the remaining 167 all came back with `429 Too Many Requests`.
Then I made it worse: a few minutes later, a test failed after just four requests, and I
briefly thought the limit was far stricter than it was. It wasn't. The window from my first
burst simply hadn't reset yet.

So I stopped guessing and measured it properly. 119 requests got through in a burst, and
access came back about 37 seconds later. That works out to roughly **120 requests per
minute**. That's why the client now limits itself to 100 per minute and backs off on 429s.

A smaller one: I first thought meters returning only 8 energy readings had truncated data.
They didn't. They just report **daily** instead of every 30 minutes.

---

## 5. If I were reviewing my own submission, what would I criticise?

- **Refreshing happens on a user's request.** When the snapshot goes stale, whichever request
  arrives next waits for the reload. A background refresh would be better.
- **Majority voting can be wrong.** It works well on this data, but if most meters on a
  transformer were mislabelled, it would confidently pick the wrong answer. Ties are broken
  arbitrarily, and nothing alerts anyone if the number of repairs suddenly jumps.
- **The tests use a fake portal.** It's built from real captured responses, so it proves my
  understanding is consistent, but it can't tell me if the real portal changes tomorrow.
- **The cache only lives in one process.** Fine for a single server, but it wouldn't be shared
  if you ran several copies of the service.
