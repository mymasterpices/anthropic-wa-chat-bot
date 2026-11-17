# Re-engagement templates for Meta approval (v1.3 — RKJ team voice)

Beyond the 24-hour customer-service window, WhatsApp only allows
pre-approved template messages. Submit these early — review takes days.
Category for all three: **MARKETING**. Submit each in **English** and
**Hindi**. Voice matches how the RKJ team actually writes on WhatsApp:
greeting first, short plain lines, no ad-speak, no pressure.

---

## 1. `shortlist_reminder`
Use: customer shortlisted pieces, then went quiet past 24h.

> Hello {{1}}, hope you are doing well.
> This is RK Jewellers, South Extension.
> The pieces you liked are still kept aside for you. Whenever you're
> ready, reply here and we'll pick up right where we left off.

Variables: {{1}} = customer first name.
Buttons (quick reply): "Show my shortlist" / "Book a visit"

Hindi version:

> Namaste {{1}} ji, aasha hai aap achhe honge.
> RK Jewellers, South Extension se.
> Jo pieces aapko pasand aaye the, humne aapke liye alag rakhe hain.
> Jab bhi aap chahein, yahin reply kar dijiye — wahin se aage badhenge.

---

## 2. `visit_invitation`
Use: customer showed interest but never fixed a visit.

> Hello {{1}}, good afternoon.
> This is RK Jewellers. We'd love to show you the pieces you liked in
> person at South Extension — trying them on makes all the difference.
> Shall we keep a convenient day for you?

Variables: {{1}} = customer first name.
Buttons (quick reply): "Yes, this week" / "Maybe later"

Hindi version:

> Namaste {{1}} ji.
> RK Jewellers se. Jo pieces aapko pasand aaye, unhe South Extension
> showroom par pehen kar dekhne ka anubhav hi alag hai.
> Aapke liye koi din tay kar dein?

---

## 3. `conversation_resume`
Use: generic re-open when a conversation lapsed before any shortlist.

> Hello {{1}}, hope you are doing well.
> This is RK Jewellers. You were exploring {{2}} with us — we have kept
> a few lovely options ready for whenever you'd like to continue.
> No hurry at all.

Variables: {{1}} = first name, {{2}} = what they were browsing
(e.g. "diamond earrings").
Buttons (quick reply): "Continue" / "Not now"

Hindi version:

> Namaste {{1}} ji, aasha hai aap achhe honge.
> RK Jewellers se. Aap humse {{2}} dekh rahe the — kuch achhe options
> humne aapke liye taiyaar rakhe hain, jab bhi aap chahein.
> Koi jaldi nahi hai.

---

**Persona guardrail:** at most one template nudge per lapsed
conversation, one more after a longer gap, then stop — mirror the
"if the customer goes quiet" rules. Track sends in the sessions DB.
