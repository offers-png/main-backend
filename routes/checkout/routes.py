/* =========================
   CREATE PAYMENT LINK
   ========================= */
app.post("/api/create-link", async (req, res) => {
  const { plan = "7d" } = req.body;
  const price = PRICE_MAP[plan];


  if (!(plan in PLAN_MAP)) {
    return res.status(400).json({ error: "Invalid plan" });
  }

  const host = req.get("x-forwarded-host") || req.get("host");
  const proto = req.get("x-forwarded-proto") || req.protocol;
  const baseUrl = `${proto}://${host}`;

  const session = await stripe.checkout.sessions.create({
    mode: "payment",
    metadata: { plan },
    line_items: [
      {
        price_data: {
          currency: "usd",
          product_data: { name: `API Access (${plan})` },
          unit_amount: price,
        },
        quantity: 1,
      },
    ],
    success_url: `${baseUrl}/success.html?session_id={CHECKOUT_SESSION_ID}`,
    cancel_url: `${baseUrl}/cancel.html`,
  });

  db.prepare(`
    INSERT INTO links (session_id, checkout_url)
    VALUES (?, ?)
  `).run(session.id, session.url);

  res.json({
    private_url: `${baseUrl}/pay/${session.id}`,
  });
});
