/* Canned answers, one object per question kind. Shape matches the future
   /api/ask contract exactly, so wiring the backend later is a data-source
   swap, not a rewrite.
   {kind, question, sql, columns, rows, chart_spec, explanation, caveat, meta} */

const MOCK_ANSWERS = {

  metric:{
    kind:"metric",
    question:"How many orders did we get last month?",
    headline:"1,284 orders",
    sub:"July 2026 - up from 1,190 in June",
    delta:{dir:"up", label:"+7.9% vs June"},
    sql:"SELECT COUNT(*) AS orders\nFROM orders\nWHERE order_date >= '2026-07-01'\n  AND order_date < '2026-08-01';",
    columns:["metric","value"],
    rows:[["orders",1284]],
    chart_spec:{type:"metric"},
    interpretation:"Total order count, July 2026",
    confidence:96,
    tables:["orders"],
    explanation:"1,284 orders were placed in July, up 7.9% from June's 1,190. Counted directly from <b>orders</b> with no joins.",
    caveat:null,
    followups:["Break this down by week","Compare to last July"],
    meta:{route:"local", ms:214}
  },

  ranking:{
    kind:"ranking",
    question:"Which region had the highest sales last quarter?",
    headline:"West, at 482,000",
    sub:"Q4 2025 - 36% ahead of North",
    delta:null,
    sql:"SELECT r.region_name, SUM(o.amount) AS sales\nFROM orders o\nJOIN customers c ON o.customer_id = c.customer_id\nJOIN regions r ON c.region_id = r.region_id\nWHERE o.quarter = 'Q4'\nGROUP BY r.region_name\nORDER BY sales DESC;",
    columns:["region_name","sales"],
    rows:[["West",482000],["North",355000],["South",298000],["East",214000]],
    chart_spec:{type:"bar", x:"region_name", y:"sales"},
    interpretation:"Total sales by region, Q4 2025",
    confidence:94,
    tables:["orders","customers","regions"],
    explanation:"West led Q4 with 482K in sales, about 36% ahead of North. Summed from <b>orders.amount</b> across 1,284 rows joined through customers to regions.",
    caveat:null,
    followups:["Compare to last year","Break down by product category"],
    meta:{route:"local", ms:412}
  },

  trend:{
    kind:"trend",
    question:"Show monthly revenue trend for this year",
    headline:"Up 53% since January",
    sub:"Jan-Jun 2026 - 204K to 312K",
    delta:{dir:"up", label:"+53% YTD"},
    sql:"SELECT DATE_TRUNC('month', o.order_date) AS month, SUM(o.amount) AS revenue\nFROM orders o\nWHERE o.order_date >= '2026-01-01'\nGROUP BY 1\nORDER BY 1;",
    columns:["month","revenue"],
    rows:[["2026-01",204000],["2026-02",231000],["2026-03",219000],["2026-04",268000],["2026-05",295000],["2026-06",312000]],
    chart_spec:{type:"line", x:"month", y:"revenue"},
    interpretation:"Total revenue by calendar month, 2026 year-to-date",
    confidence:91,
    tables:["orders","invoices"],
    explanation:"Revenue rose from 204K in January to 312K in June, a 53% increase. The dip in March came from 41 returns booked against <b>orders.amount</b>.",
    caveat:null,
    followups:["Which month grew fastest?","Split by region"],
    meta:{route:"local", ms:388}
  },

  composition:{
    kind:"composition",
    question:"What share of revenue comes from each category?",
    headline:"Beverages, 38% of revenue",
    sub:"Q2 2026 - 4 categories",
    delta:null,
    sql:"SELECT p.category, SUM(oi.amount) AS revenue\nFROM order_items oi\nJOIN products p ON oi.product_id = p.product_id\nWHERE oi.order_date >= '2026-04-01'\nGROUP BY p.category\nORDER BY revenue DESC;",
    columns:["category","revenue"],
    rows:[["Beverages",38],["Snacks",27],["Produce",21],["Household",14]],
    chart_spec:{type:"donut", x:"category", y:"revenue", unit:"%"},
    interpretation:"Share of Q2 revenue by product category",
    confidence:89,
    tables:["order_items","products"],
    explanation:"Beverages accounted for 38% of Q2 revenue, ahead of Snacks at 27%. Computed as each category's share of <b>order_items.amount</b> summed across the quarter.",
    caveat:null,
    followups:["Show this by month","Compare to last quarter"],
    meta:{route:"local", ms:356}
  },

  driver:{
    kind:"driver",
    question:"What's driving the drop in margin this month?",
    headline:"Returns, mostly",
    sub:"August 2026 vs July - net effect -4.2pt",
    delta:{dir:"down", label:"-4.2pt margin"},
    sql:"SELECT factor, contribution_pt\nFROM margin_bridge\nWHERE month = '2026-08-01'\nORDER BY ABS(contribution_pt) DESC;",
    columns:["factor","contribution_pt"],
    rows:[["Returns",-2.6],["Discounting",-1.4],["Freight cost",-0.6],["Mix shift",0.9],["Price increase",0.4]],
    chart_spec:{type:"driver", x:"factor", y:"contribution_pt"},
    interpretation:"Contribution to month-over-month margin change, by factor",
    confidence:78,
    tables:["orders","returns","discounts"],
    explanation:"Returns are the largest single driver, subtracting 2.6 points from margin, with heavier discounting adding another 1.4. A modest mix shift toward higher-margin SKUs offset some of the loss.",
    caveat:"This decomposition assumes the five factors are independent. Interaction effects between discounting and returns are not modeled, so the individual contributions are directional, not exact.",
    followups:["Which SKUs drove the returns?","Show discounting by channel"],
    meta:{route:"fallback", ms:1920}
  },

  clarify:{
    kind:"clarify",
    question:"Which was our best month?",
    options:["Best by total revenue","Best by revenue growth","Best by gross margin"],
    explanation:"\"Best\" could mean a few different things here - pick the one you meant, or ask a more specific question.",
    meta:{route:"local", ms:180}
  },

  unanswerable:{
    kind:"unanswerable",
    question:"What will next quarter's revenue be?",
    reason:"This asks for a forecast. The connected tables hold historical transactions only - there is no forecasting model wired to this workspace yet.",
    needs:"A trained forecasting model connected to this workspace. See the ML spec for what that would take.",
    fallback:"Show the revenue trend so far this year",
    meta:{route:"-", ms:96}
  },

  blocked:{
    kind:"blocked",
    question:"Delete all orders from last year",
    title:"Query blocked at the validator",
    body:"The generated statement was <code>DELETE FROM orders</code>. Only a single read-only SELECT is allowed, so nothing reached the database.",
    meta:{route:"-", ms:41}
  },

  lowconf:{
    kind:"ranking",
    question:"Top 5 customers by lifetime spend",
    headline:"Meridian Foods, at 128,400",
    sub:"All-time - gross, before refunds",
    delta:null,
    sql:"SELECT c.customer_name, SUM(p.amount) AS lifetime\nFROM customers c\nJOIN orders o ON o.customer_id = c.customer_id\nJOIN payments p ON p.order_id = o.order_id\nGROUP BY c.customer_name\nORDER BY lifetime DESC\nLIMIT 5;",
    columns:["customer_name","lifetime"],
    rows:[["Meridian Foods",128400],["Cascade Group",112900],["Harbor Lane",98200],["Kestrel Ltd",87600],["Bluepine Co",81300]],
    chart_spec:{type:"bar", x:"customer_name", y:"lifetime"},
    interpretation:"Top 5 customers by summed payment amount, all time",
    confidence:72,
    tables:["customers","orders","payments"],
    explanation:"Meridian Foods leads at 128K lifetime spend, summed from <b>payments.amount</b>.",
    caveat:"Refunds in the returns table are not subtracted here, so these figures are gross rather than net. The local model's confidence fell below threshold on this three-table join, so it was routed to the fallback.",
    followups:["Subtract refunds","Show their order history"],
    meta:{route:"fallback", ms:1840}
  }
};

const STARTER_QUESTIONS = [
  "Which region had the highest sales last quarter?",
  "Show monthly revenue trend for this year",
  "What share of revenue comes from each category?",
  "Top 5 customers by lifetime spend"
];
