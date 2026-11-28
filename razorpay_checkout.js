/*
  razorpay_checkout.js - peyment.html madhe Cashfree chya JS chya jagi he taka.

  1) peyment.html chya </body> adhi ya 2 oli taka:
       <script src="https://checkout.razorpay.com/v1/checkout.js"></script>
       <script src="razorpay_checkout.js"></script>   (kinva ya file cha code thet paste kara)

  2) Pay button var:  <button onclick="payWithRazorpay('starter')">Pay Now</button>
     'starter' / 'pro' he razorpay_gateway.py madhlya PLANS madhle keys aahet.
*/
async function payWithRazorpay(plan, userRef) {
  try {
    const res = await fetch("/api/razorpay/create-order", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan: plan, user_ref: userRef || "" }),
    });
    const order = await res.json();
    if (!order.ok) {
      alert("Payment start hot nahi: " + (order.error || "unknown error"));
      console.error(order);
      return;
    }

    const options = {
      key: order.key_id,
      amount: order.amount,
      currency: order.currency,
      name: "GW Smart Analytics",
      description: order.name,
      order_id: order.order_id,
      theme: { color: "#2563eb" },
      handler: async function (response) {
        // Payment zali -> server var signature verify karaycha
        const v = await fetch("/api/razorpay/verify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            razorpay_order_id: response.razorpay_order_id,
            razorpay_payment_id: response.razorpay_payment_id,
            razorpay_signature: response.razorpay_signature,
          }),
        });
        const result = await v.json();
        if (result.ok) {
          alert("Payment successful! Payment ID: " + result.payment_id);
          // ITHE: tumcha existing success UI / receipt page / redirect call kara
          // window.location.href = "/dashboard";
        } else {
          alert("Payment verify jhali nahi. Support shi contact kara. " + (result.error || ""));
        }
      },
      modal: {
        ondismiss: function () {
          console.log("Payment popup band kela");
        },
      },
    };

    const rzp = new Razorpay(options);
    rzp.on("payment.failed", function (resp) {
      alert("Payment failed: " + resp.error.description);
      console.error(resp.error);
    });
    rzp.open();
  } catch (err) {
    console.error(err);
    alert("Kahi tari chukle: " + err.message);
  }
}


/*
  PRATYEK DASHBOARD SATHI Rs 10:
  Dashboard generate/download karnya adhi:
      payForDashboard(dashboardId, function () { ...dashboard dakhva / download kara... });
  dashboardId = tya dashboard cha unique id (uuid, file hash, kinva DB id).
  Ekach dashboardId sathi dusrya veli pay karaycha nahi, ti already paid asel tar direct onPaid() chalto.
*/
async function payForDashboard(dashboardId, onPaid) {
  const chk = await (await fetch("/api/razorpay/is-paid/" + encodeURIComponent(dashboardId))).json();
  if (chk.paid) { onPaid(); return; }

  const res = await fetch("/api/razorpay/create-order", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan: "dashboard", dashboard_id: dashboardId }),
  });
  const order = await res.json();
  if (!order.ok) { alert("Payment start hot nahi: " + (order.error || "")); return; }

  const rzp = new Razorpay({
    key: order.key_id, amount: order.amount, currency: order.currency,
    name: "GW Smart Analytics", description: "Unique Dashboard - Rs 10",
    order_id: order.order_id, theme: { color: "#2563eb" },
    handler: async function (r) {
      const v = await (await fetch("/api/razorpay/verify", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          razorpay_order_id: r.razorpay_order_id,
          razorpay_payment_id: r.razorpay_payment_id,
          razorpay_signature: r.razorpay_signature,
        }),
      })).json();
      if (v.ok) onPaid(); else alert("Verify jhali nahi: " + (v.error || ""));
    },
  });
  rzp.on("payment.failed", function (e) { alert("Payment failed: " + e.error.description); });
  rzp.open();
}
