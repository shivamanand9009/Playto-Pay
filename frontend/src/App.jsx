import { useState, useEffect, useCallback, useRef } from "react";

const BACKEND = "http://localhost:8000/api/v1";

const paise2Rupees = (p) => {
  if (p === null || p === undefined) return "₹0.00";
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: 2,
  }).format(p / 100);
};

const relativeTime = (iso) => {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ago`;
};

const generateUUID = () =>
  "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });

const STATUS_CFG = {
  pending: { label: "Pending", bg: "#2d1f00", text: "#fbbf24", dot: "#f59e0b" },
  processing: {
    label: "Processing",
    bg: "#0d1f3c",
    text: "#93c5fd",
    dot: "#3b82f6",
  },
  completed: {
    label: "Completed",
    bg: "#052e16",
    text: "#6ee7b7",
    dot: "#10b981",
  },
  failed: { label: "Failed", bg: "#2d0f0f", text: "#fca5a5", dot: "#ef4444" },
};

const StatusBadge = ({ status }) => {
  const cfg = STATUS_CFG[status] || STATUS_CFG.pending;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "3px 10px",
        borderRadius: 999,
        background: cfg.bg,
        color: cfg.text,
        fontSize: 11,
        fontWeight: 600,
        whiteSpace: "nowrap",
      }}
    >
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: "50%",
          background: cfg.dot,
          display: "inline-block",
          flexShrink: 0,
        }}
      />
      {cfg.label}
    </span>
  );
};

export default function App() {
  const [merchants, setMerchants] = useState([]);
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [dashboard, setDashboard] = useState(null);
  const [loadingMerchants, setLoadingMerchants] = useState(true);
  const [loadingDashboard, setLoadingDashboard] = useState(false);
  const [payoutForm, setPayoutForm] = useState({
    amount: "",
    submitting: false,
    error: "",
    success: "",
  });
  const [mobileTab, setMobileTab] = useState("payouts");
  const pollRef = useRef(null);

  useEffect(() => {
    fetch(`${BACKEND}/merchants/`)
      .then((r) => r.json())
      .then((data) => {
        setMerchants(data);
        setLoadingMerchants(false);
      })
      .catch(() => setLoadingMerchants(false));
  }, []);

  const fetchDashboard = useCallback(() => {
    if (!merchants[selectedIdx]) return;
    const id = merchants[selectedIdx].id;
    setLoadingDashboard(true);
    fetch(`${BACKEND}/merchants/${id}/`)
      .then((r) => r.json())
      .then((data) => {
        setDashboard(data);
        setMerchants((prev) =>
          prev.map((m, i) =>
            i === selectedIdx
              ? {
                  ...m,
                  available_balance_paise: data.balance.available_paise,
                  held_balance_paise: data.balance.held_paise,
                }
              : m,
          ),
        );
        setLoadingDashboard(false);
      })
      .catch(() => setLoadingDashboard(false));
  }, [merchants, selectedIdx]);

  useEffect(() => {
    if (merchants.length > 0) fetchDashboard();
  }, [selectedIdx, merchants.length]);

  useEffect(() => {
    clearInterval(pollRef.current);
    pollRef.current = setInterval(fetchDashboard, 3000);
    return () => clearInterval(pollRef.current);
  }, [fetchDashboard]);

  const handleSubmitPayout = async () => {
    const amountRupees = parseFloat(payoutForm.amount);
    if (!amountRupees || amountRupees <= 0) {
      setPayoutForm((f) => ({ ...f, error: "Enter a valid amount" }));
      return;
    }
    const amountPaise = Math.round(amountRupees * 100);
    const merchant = merchants[selectedIdx];
    if (amountPaise > merchant.available_balance_paise) {
      setPayoutForm((f) => ({ ...f, error: "Insufficient available balance" }));
      return;
    }
    setPayoutForm((f) => ({ ...f, submitting: true, error: "", success: "" }));
    try {
      const resp = await fetch(`${BACKEND}/merchants/${merchant.id}/payouts/`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": generateUUID(),
        },
        body: JSON.stringify({
          amount_paise: amountPaise,
          bank_account_id: merchant.bank_account_id,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        setPayoutForm((f) => ({
          ...f,
          submitting: false,
          error: data.error || "Request failed",
        }));
        return;
      }
      setPayoutForm({
        amount: "",
        submitting: false,
        error: "",
        success: `Payout of ${paise2Rupees(amountPaise)} queued!`,
      });
      setTimeout(() => setPayoutForm((f) => ({ ...f, success: "" })), 4000);
      setMobileTab("payouts");
      fetchDashboard();
    } catch {
      setPayoutForm((f) => ({
        ...f,
        submitting: false,
        error: "Network error — is the backend running?",
      }));
    }
  };

  const merchant = merchants[selectedIdx];
  const balance = dashboard?.balance;
  const ledger = dashboard?.ledger || [];
  const payouts = dashboard?.payouts || [];

  const credits = ledger
    .filter((e) => e.entry_type === "credit")
    .reduce((s, e) => s + e.amount_paise, 0);
  const debits = ledger
    .filter((e) => e.entry_type === "debit")
    .reduce((s, e) => s + e.amount_paise, 0);
  const displayTotal =
    (balance?.available_paise || 0) + (balance?.held_paise || 0);
  const invariantOk = Math.abs(credits - debits - displayTotal) < 2;

  return (
    <div
      style={{
        fontFamily: "'DM Mono','Courier New',monospace",
        minHeight: "100vh",
        background: "#0a0b0f",
        color: "#e2e8f0",
      }}
    >
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@300;400;500&family=Bebas+Neue&display=swap');
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body { overflow-x: hidden; }
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: #0a0b0f; }
        ::-webkit-scrollbar-thumb { background: #2d3748; border-radius: 2px; }
        .ticker { animation: ticker 1s steps(1) infinite; }
        @keyframes ticker { 50% { opacity: 0; } }
        .fade-in { animation: fadeIn 0.3s ease; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(-6px); } to { opacity: 1; transform: translateY(0); } }
        .card { background: #111318; border: 1px solid #1e2330; border-radius: 8px; }
        .input-field {
          background: #0d0f14; border: 1px solid #2d3748; border-radius: 6px;
          color: #e2e8f0; padding: 10px 14px; width: 100%; outline: none;
          font-family: inherit; font-size: 14px; transition: border-color 0.2s;
        }
        .input-field:focus { border-color: #10b981; }
        .btn-primary {
          background: #10b981; color: #0a0b0f; font-weight: 600; padding: 12px 20px;
          border: none; border-radius: 6px; cursor: pointer; font-family: inherit;
          font-size: 13px; letter-spacing: 0.05em; transition: all 0.2s; width: 100%;
        }
        .btn-primary:hover:not(:disabled) { background: #059669; }
        .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
        .shimmer {
          background: linear-gradient(90deg, #111318 25%, #1a1f2e 50%, #111318 75%);
          background-size: 200% 100%; animation: shimmer 1.5s infinite; border-radius: 4px;
        }
        @keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }

        /* Desktop layout */
        .layout { display: grid; grid-template-columns: 300px 1fr; gap: 20px; }
        .balance-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 20px; }
        .mobile-tabs { display: none; }
        .panel-payouts, .panel-ledger { display: block; }

        /* Tablet */
        @media (max-width: 860px) {
          .layout { grid-template-columns: 1fr; }
          .balance-row { grid-template-columns: repeat(3, 1fr); gap: 10px; }
          .mobile-tabs { display: flex; border-bottom: 1px solid #1e2330; margin-bottom: 0; }
          .panel-payouts { display: var(--show-payouts, block); }
          .panel-ledger  { display: var(--show-ledger, none); }
        }

        /* Phone */
        @media (max-width: 500px) {
          .balance-row { grid-template-columns: 1fr 1fr; }
          .balance-total { display: none; }
          .pad { padding: 16px !important; }
          .hpad { padding: 0 16px !important; }
          .balance-amount { font-size: 22px !important; }
        }
      `}</style>

      {/* Header */}
      <header style={{ borderBottom: "1px solid #1e2330" }}>
        <div
          className="hpad"
          style={{
            maxWidth: 1200,
            margin: "0 auto",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            height: 54,
            padding: "0 32px",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div
              style={{
                width: 26,
                height: 26,
                background: "#10b981",
                borderRadius: 5,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
              }}
            >
              <span style={{ fontSize: 13, fontWeight: 700, color: "#0a0b0f" }}>
                ₹
              </span>
            </div>
            <span
              style={{
                fontFamily: "'Bebas Neue',sans-serif",
                fontSize: 20,
                letterSpacing: "0.1em",
                color: "#f1f5f9",
              }}
            >
              PAYOUTPRO
            </span>
            <span
              style={{
                fontSize: 9,
                color: "#4b5563",
                borderLeft: "1px solid #1e2330",
                paddingLeft: 10,
                letterSpacing: "0.12em",
              }}
            >
              MERCHANT CONSOLE
            </span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span className="ticker" style={{ color: "#10b981", fontSize: 10 }}>
              ●
            </span>
            <span
              style={{
                fontSize: 10,
                color: "#4b5563",
                letterSpacing: "0.08em",
              }}
            >
              LIVE · 3s
            </span>
          </div>
        </div>
      </header>

      <div
        className="pad"
        style={{ maxWidth: 1200, margin: "0 auto", padding: "20px 32px" }}
      >
        {/* Merchant tabs — scrollable */}
        <div
          style={{
            display: "flex",
            gap: 8,
            marginBottom: 18,
            overflowX: "auto",
            paddingBottom: 4,
            WebkitOverflowScrolling: "touch",
            scrollbarWidth: "none",
          }}
        >
          <style>{`.merchant-scroll::-webkit-scrollbar{display:none}`}</style>
          {loadingMerchants
            ? [1, 2, 3].map((i) => (
                <div
                  key={i}
                  className="shimmer"
                  style={{
                    width: 120,
                    height: 34,
                    flexShrink: 0,
                    borderRadius: 6,
                  }}
                />
              ))
            : merchants.map((m, i) => (
                <button
                  key={m.id}
                  onClick={() => {
                    setSelectedIdx(i);
                    setDashboard(null);
                    setPayoutForm({
                      amount: "",
                      submitting: false,
                      error: "",
                      success: "",
                    });
                  }}
                  style={{
                    flexShrink: 0,
                    padding: "7px 14px",
                    borderRadius: 6,
                    border: "1px solid",
                    fontSize: 12,
                    fontFamily: "inherit",
                    cursor: "pointer",
                    transition: "all 0.15s",
                    background: i === selectedIdx ? "#10b981" : "transparent",
                    color: i === selectedIdx ? "#0a0b0f" : "#64748b",
                    borderColor: i === selectedIdx ? "#10b981" : "#1e2330",
                    fontWeight: i === selectedIdx ? 600 : 400,
                  }}
                >
                  {m.name}
                </button>
              ))}
        </div>

        {/* Balance cards */}
        <div className="balance-row">
          {[
            {
              label: "AVAILABLE",
              value: balance?.available_paise,
              accent: "#10b981",
              sub: "Ready to withdraw",
              cls: "",
            },
            {
              label: "HELD",
              value: balance?.held_paise,
              accent: "#f59e0b",
              sub: "Pending payouts",
              cls: "",
            },
            {
              label: "TOTAL",
              value: balance
                ? balance.available_paise + balance.held_paise
                : null,
              accent: "#6366f1",
              sub: "Avail + held",
              cls: "balance-total",
            },
          ].map(({ label, value, accent, sub, cls }) => (
            <div
              key={label}
              className={`card ${cls}`}
              style={{ padding: "14px 16px" }}
            >
              <div
                style={{
                  fontSize: 9,
                  color: "#4b5563",
                  letterSpacing: "0.15em",
                  marginBottom: 6,
                }}
              >
                {label}
              </div>
              {value === null || value === undefined ? (
                <div
                  className="shimmer"
                  style={{ height: 28, width: "75%", marginBottom: 4 }}
                />
              ) : (
                <div
                  className="balance-amount"
                  style={{
                    fontFamily: "'Bebas Neue',sans-serif",
                    fontSize: 26,
                    color: accent,
                    lineHeight: 1,
                  }}
                >
                  {paise2Rupees(value)}
                </div>
              )}
              <div style={{ fontSize: 10, color: "#374151", marginTop: 4 }}>
                {sub}
              </div>
            </div>
          ))}
        </div>

        {/* Main layout */}
        <div className="layout">
          {/* Left column */}
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {/* Payout form */}
            <div className="card" style={{ padding: 18 }}>
              <div
                style={{
                  fontSize: 10,
                  color: "#4b5563",
                  letterSpacing: "0.15em",
                  marginBottom: 14,
                }}
              >
                REQUEST PAYOUT
              </div>

              <label
                style={{
                  fontSize: 11,
                  color: "#64748b",
                  display: "block",
                  marginBottom: 5,
                }}
              >
                Amount (₹)
              </label>
              <input
                className="input-field"
                type="number"
                placeholder="0.00"
                value={payoutForm.amount}
                onChange={(e) =>
                  setPayoutForm((f) => ({
                    ...f,
                    amount: e.target.value,
                    error: "",
                  }))
                }
                onKeyDown={(e) => e.key === "Enter" && handleSubmitPayout()}
                style={{ marginBottom: 12 }}
              />

              <label
                style={{
                  fontSize: 11,
                  color: "#64748b",
                  display: "block",
                  marginBottom: 5,
                }}
              >
                Bank Account
              </label>
              <input
                className="input-field"
                readOnly
                value={merchant?.bank_account_id || ""}
                style={{ opacity: 0.6, marginBottom: 14 }}
              />

              {payoutForm.error && (
                <div
                  style={{
                    fontSize: 12,
                    color: "#ef4444",
                    marginBottom: 12,
                    padding: "8px 12px",
                    background: "#1f0f0f",
                    borderRadius: 6,
                  }}
                >
                  ⚠ {payoutForm.error}
                </div>
              )}
              {payoutForm.success && (
                <div
                  className="fade-in"
                  style={{
                    fontSize: 12,
                    color: "#10b981",
                    marginBottom: 12,
                    padding: "8px 12px",
                    background: "#0a1f16",
                    borderRadius: 6,
                  }}
                >
                  ✓ {payoutForm.success}
                </div>
              )}

              <button
                className="btn-primary"
                onClick={handleSubmitPayout}
                disabled={payoutForm.submitting || !merchant}
              >
                {payoutForm.submitting ? "PROCESSING..." : "REQUEST PAYOUT →"}
              </button>
              <div
                style={{
                  fontSize: 10,
                  color: "#374151",
                  marginTop: 8,
                  textAlign: "center",
                }}
              >
                Auto idempotency key · Funds held immediately
              </div>
            </div>

            {/* Invariant */}
            {dashboard && (
              <div className="card" style={{ padding: 14 }}>
                <div
                  style={{
                    fontSize: 9,
                    color: "#4b5563",
                    letterSpacing: "0.12em",
                    marginBottom: 10,
                  }}
                >
                  BALANCE INVARIANT
                </div>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    fontSize: 11,
                    marginBottom: 4,
                  }}
                >
                  <span style={{ color: "#64748b" }}>Σ credits</span>
                  <span style={{ color: "#10b981" }}>
                    {paise2Rupees(credits)}
                  </span>
                </div>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    fontSize: 11,
                    marginBottom: 4,
                  }}
                >
                  <span style={{ color: "#64748b" }}>Σ debits</span>
                  <span style={{ color: "#ef4444" }}>
                    {paise2Rupees(debits)}
                  </span>
                </div>
                <div
                  style={{ borderTop: "1px solid #1e2330", margin: "8px 0" }}
                />
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    fontSize: 11,
                  }}
                >
                  <span style={{ color: "#64748b" }}>Invariant</span>
                  <span
                    style={{
                      color: invariantOk ? "#10b981" : "#ef4444",
                      fontWeight: 600,
                    }}
                  >
                    {invariantOk ? "✓ HOLDS" : "✗ CHECK API"}
                  </span>
                </div>
              </div>
            )}
          </div>

          {/* Right column */}
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 14,
              minWidth: 0,
            }}
            style={{
              "--show-payouts": mobileTab === "payouts" ? "block" : "none",
              "--show-ledger": mobileTab === "ledger" ? "block" : "none",
              display: "flex",
              flexDirection: "column",
              gap: 14,
              minWidth: 0,
            }}
          >
            {/* Mobile tab switcher */}
            <div className="mobile-tabs">
              {["payouts", "ledger"].map((t) => (
                <button
                  key={t}
                  onClick={() => setMobileTab(t)}
                  style={{
                    padding: "10px 18px",
                    border: "none",
                    background: "transparent",
                    fontFamily: "inherit",
                    fontSize: 11,
                    cursor: "pointer",
                    letterSpacing: "0.1em",
                    transition: "all 0.15s",
                    color: mobileTab === t ? "#10b981" : "#4b5563",
                    borderBottom: `2px solid ${mobileTab === t ? "#10b981" : "transparent"}`,
                  }}
                >
                  {t.toUpperCase()}
                </button>
              ))}
            </div>

            {/* Payout history */}
            <div
              className="card panel-payouts"
              style={{ overflow: "hidden", minWidth: 0 }}
            >
              <div
                style={{
                  padding: "13px 16px",
                  borderBottom: "1px solid #1e2330",
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                }}
              >
                <span
                  style={{
                    fontSize: 10,
                    color: "#4b5563",
                    letterSpacing: "0.15em",
                  }}
                >
                  PAYOUT HISTORY
                </span>
                <span style={{ fontSize: 11, color: "#374151" }}>
                  {payouts.length} payouts
                </span>
              </div>
              <div style={{ maxHeight: 300, overflowY: "auto" }}>
                {loadingDashboard && payouts.length === 0 ? (
                  [1, 2, 3].map((i) => (
                    <div
                      key={i}
                      className="shimmer"
                      style={{ margin: "12px 16px", height: 44 }}
                    />
                  ))
                ) : payouts.length === 0 ? (
                  <div
                    style={{
                      padding: 32,
                      textAlign: "center",
                      color: "#374151",
                      fontSize: 13,
                    }}
                  >
                    No payouts yet — request one!
                  </div>
                ) : (
                  payouts.map((p) => (
                    <div
                      key={p.id}
                      style={{
                        padding: "11px 16px",
                        borderBottom: "1px solid #0f1219",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: 10,
                      }}
                    >
                      <div
                        style={{
                          display: "flex",
                          flexDirection: "column",
                          gap: 4,
                          minWidth: 0,
                        }}
                      >
                        <div
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 8,
                            flexWrap: "wrap",
                          }}
                        >
                          <span
                            style={{
                              fontFamily: "'Bebas Neue'",
                              fontSize: 15,
                              color: "#f59e0b",
                              flexShrink: 0,
                            }}
                          >
                            {paise2Rupees(p.amount_paise)}
                          </span>
                          <StatusBadge status={p.status} />
                          {p.attempt_count > 1 && (
                            <span style={{ fontSize: 10, color: "#6366f1" }}>
                              attempt {p.attempt_count}
                            </span>
                          )}
                        </div>
                        <div
                          style={{
                            fontSize: 10,
                            color: "#4b5563",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {p.bank_account_id} · {relativeTime(p.created_at)}
                          {p.failure_reason && (
                            <span style={{ color: "#ef4444" }}>
                              {" "}
                              · {p.failure_reason}
                            </span>
                          )}
                        </div>
                      </div>
                      <div
                        style={{
                          fontSize: 10,
                          color: "#374151",
                          fontFamily: "monospace",
                          flexShrink: 0,
                        }}
                      >
                        #{p.id.slice(0, 8)}
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>

            {/* Ledger */}
            <div
              className="card panel-ledger"
              style={{ overflow: "hidden", minWidth: 0 }}
            >
              <div
                style={{
                  padding: "13px 16px",
                  borderBottom: "1px solid #1e2330",
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                }}
              >
                <span
                  style={{
                    fontSize: 10,
                    color: "#4b5563",
                    letterSpacing: "0.15em",
                  }}
                >
                  LEDGER
                </span>
                <span style={{ fontSize: 11, color: "#374151" }}>
                  {ledger.length} entries
                </span>
              </div>
              <div style={{ maxHeight: 300, overflowY: "auto" }}>
                {loadingDashboard && ledger.length === 0
                  ? [1, 2, 3, 4].map((i) => (
                      <div
                        key={i}
                        className="shimmer"
                        style={{ margin: "10px 16px", height: 36 }}
                      />
                    ))
                  : ledger.map((entry) => (
                      <div
                        key={entry.id}
                        style={{
                          padding: "9px 16px",
                          borderBottom: "1px solid #0f1219",
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          gap: 10,
                        }}
                      >
                        <div
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 10,
                            minWidth: 0,
                          }}
                        >
                          <div
                            style={{
                              width: 6,
                              height: 6,
                              borderRadius: "50%",
                              background:
                                entry.entry_type === "credit"
                                  ? "#10b981"
                                  : "#ef4444",
                              flexShrink: 0,
                            }}
                          />
                          <div style={{ minWidth: 0 }}>
                            <div
                              style={{
                                fontSize: 12,
                                color: "#94a3b8",
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                whiteSpace: "nowrap",
                              }}
                            >
                              {entry.description}
                            </div>
                            <div
                              style={{
                                fontSize: 10,
                                color: "#374151",
                                marginTop: 2,
                              }}
                            >
                              {relativeTime(entry.created_at)}
                            </div>
                          </div>
                        </div>
                        <span
                          style={{
                            fontSize: 13,
                            fontFamily: "'Bebas Neue'",
                            color:
                              entry.entry_type === "credit"
                                ? "#10b981"
                                : "#ef4444",
                            flexShrink: 0,
                          }}
                        >
                          {entry.entry_type === "credit" ? "+" : "−"}
                          {paise2Rupees(entry.amount_paise)}
                        </span>
                      </div>
                    ))}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Footer */}
      <footer
        style={{
          borderTop: "1px solid #1e2330",
          padding: "12px 32px",
          marginTop: 20,
        }}
      >
        <div
          style={{
            maxWidth: 1200,
            margin: "0 auto",
            display: "flex",
            justifyContent: "space-between",
            flexWrap: "wrap",
            gap: 4,
            fontSize: 9,
            color: "#374151",
            letterSpacing: "0.08em",
          }}
        >
          <span>PAYOUTPRO · AMOUNTS IN PAISE · NO FLOATS</span>
          <span>SELECT FOR UPDATE · IDEMPOTENT · ATOMIC</span>
        </div>
      </footer>
    </div>
  );
}
