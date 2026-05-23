"use client";

import { useState, useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { format } from "date-fns";
import { fr } from "date-fns/locale";
import { NotebookPen, Plus } from "lucide-react";
import { matchesApi, betsApi } from "@/lib/api";
import { useAuthStore } from "@/store/auth";
import { formatCurrency, cn } from "@/lib/utils";

interface UpcomingMatch {
  id: string;
  sport: string;
  league: string;
  home_team: string;
  away_team: string;
  match_date: string;
  home_odds: number | null;
  draw_odds: number | null;
  away_odds: number | null;
  over_25_odds: number | null;
  under_25_odds: number | null;
  ah_line: number | null;
  ah_home_odds: number | null;
  ah_away_odds: number | null;
}

// Format un handicap signé : 1.5 -> "+1.5", -1.5 -> "-1.5"
function fmtHandicap(line: number): string {
  return `${line > 0 ? "+" : ""}${line}`;
}

interface Bet {
  id: string;
  outcome: string;
  amount: number;
  odds: number;
  status: string;
  profit_loss: number | null;
  bookmaker: string | null;
  placed_at: string;
  match: {
    home_team: string;
    away_team: string;
    league: string;
    match_date: string;
  } | null;
}

// Outcomes proposés selon le sport + les cotes disponibles sur le match
function outcomeOptions(m: UpcomingMatch): { value: string; label: string; odds: number | null }[] {
  const isNba = m.sport === "NBA";
  const opts: { value: string; label: string; odds: number | null }[] = [];
  if (m.home_odds) opts.push({ value: "HOME", label: m.home_team, odds: m.home_odds });
  if (!isNba && m.draw_odds) opts.push({ value: "DRAW", label: "Match nul", odds: m.draw_odds });
  if (m.away_odds) opts.push({ value: "AWAY", label: m.away_team, odds: m.away_odds });
  if (m.over_25_odds)
    opts.push({ value: "OVER", label: isNba ? "Over (points)" : "+2.5 buts", odds: m.over_25_odds });
  if (m.under_25_odds)
    opts.push({ value: "UNDER", label: isNba ? "Under (points)" : "-2.5 buts", odds: m.under_25_odds });
  // Asian Handicap (foot uniquement) : ah_line est le handicap côté domicile,
  // l'extérieur prend le handicap opposé (-ah_line).
  if (!isNba && m.ah_line !== null && m.ah_home_odds)
    opts.push({
      value: "AH_HOME",
      label: `${m.home_team} ${fmtHandicap(m.ah_line)}`,
      odds: m.ah_home_odds,
    });
  if (!isNba && m.ah_line !== null && m.ah_away_odds)
    opts.push({
      value: "AH_AWAY",
      label: `${m.away_team} ${fmtHandicap(-m.ah_line)}`,
      odds: m.ah_away_odds,
    });
  return opts;
}

const STATUS_LABELS: Record<string, string> = {
  PENDING: "En attente", WON: "Gagné", LOST: "Perdu", VOID: "Annulé",
};

const OUTCOME_LABELS: Record<string, string> = {
  HOME: "Domicile", DRAW: "Nul", AWAY: "Extérieur",
  OVER: "Over", UNDER: "Under",
  AH_HOME: "Handicap dom.", AH_AWAY: "Handicap ext.",
};

export default function JournalPage() {
  const qc = useQueryClient();
  const { user, updateUser } = useAuthStore();

  const [matchId, setMatchId] = useState("");
  const [outcome, setOutcome] = useState("");
  const [odds, setOdds] = useState("");
  const [amount, setAmount] = useState("");
  const [bookmaker, setBookmaker] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // Matchs sélectionnables : 14 jours à venir, foot + NBA
  const { data: matches = [] } = useQuery<UpcomingMatch[]>({
    queryKey: ["journal-upcoming"],
    queryFn: () => matchesApi.upcoming(undefined, 200, 14).then((r) => r.data),
  });

  const { data: bets = [], refetch } = useQuery<Bet[]>({
    queryKey: ["bets", "PENDING"],
    queryFn: () => betsApi.list("PENDING").then((r) => r.data),
  });

  const selectedMatch = useMemo(
    () => matches.find((m) => m.id === matchId),
    [matches, matchId],
  );
  const options = selectedMatch ? outcomeOptions(selectedMatch) : [];

  // Quand on choisit un outcome, préremplit la cote du marché (modifiable)
  function pickOutcome(value: string) {
    setOutcome(value);
    const opt = options.find((o) => o.value === value);
    if (opt?.odds) setOdds(opt.odds.toFixed(2));
  }

  async function save() {
    setError(null);
    setSuccess(null);
    const amt = parseFloat(amount);
    const od = parseFloat(odds);
    if (!matchId || !outcome) { setError("Choisis un match et un type de pari."); return; }
    if (!amt || amt <= 0) { setError("Montant invalide."); return; }
    if (!od || od <= 1) { setError("Cote invalide (doit être > 1)."); return; }
    if (user && user.bankroll < amt) { setError("Bankroll insuffisante."); return; }

    setSaving(true);
    try {
      await betsApi.create({
        match_id: matchId,
        outcome,
        amount: amt,
        odds: od,
        bookmaker: bookmaker || undefined,
      });
      if (user) updateUser({ bankroll: Math.max(0, user.bankroll - amt) });
      qc.invalidateQueries({ queryKey: ["bankroll"] });
      qc.invalidateQueries({ queryKey: ["bets"] });
      setSuccess("Pari enregistré ✓");
      // Reset partiel (garde le match sélectionné pour enchaîner)
      setOutcome(""); setOdds(""); setAmount(""); setBookmaker("");
      refetch();
    } catch (e) {
      const err = e as { response?: { status?: number; data?: { detail?: string } } };
      setError(err?.response?.data?.detail || "Erreur lors de l'enregistrement.");
    } finally {
      setSaving(false);
    }
  }

  async function settle(betId: string, status: string) {
    await betsApi.updateResult(betId, { status });
    qc.invalidateQueries({ queryKey: ["bankroll"] });
    qc.invalidateQueries({ queryKey: ["bets"] });
    qc.invalidateQueries({ queryKey: ["stats"] });
    refetch();
  }

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <NotebookPen className="w-6 h-6 text-brand-500" />
          Mes paris
        </h1>
        <p className="text-sm text-gray-400 mt-1">
          Enregistre les paris que tu places toi-même (n&apos;importe quel type),
          puis marque le résultat. C&apos;est ton journal pour mesurer ta vraie performance.
        </p>
      </div>

      {/* Formulaire de saisie */}
      <div className="card space-y-4">
        <h2 className="font-semibold flex items-center gap-2">
          <Plus className="w-4 h-4 text-brand-400" /> Ajouter un pari
        </h2>

        {/* 1. Match */}
        <div>
          <label className="text-xs text-gray-500 block mb-1">Match</label>
          <select
            className="input w-full"
            value={matchId}
            onChange={(e) => { setMatchId(e.target.value); setOutcome(""); setOdds(""); }}
          >
            <option value="">— Choisis un match —</option>
            {matches.map((m) => (
              <option key={m.id} value={m.id}>
                [{m.sport === "NBA" ? "NBA" : m.league}] {m.home_team} vs {m.away_team} — {format(new Date(m.match_date), "d MMM HH:mm", { locale: fr })}
              </option>
            ))}
          </select>
          {matches.length === 0 && (
            <p className="text-xs text-gray-500 mt-1">
              Aucun match à venir dans les 14 prochains jours en base.
            </p>
          )}
        </div>

        {/* 2. Type de pari */}
        {selectedMatch && (
          <div>
            <label className="text-xs text-gray-500 block mb-1">Type de pari</label>
            <div className="flex flex-wrap gap-2">
              {options.map((o) => (
                <button
                  key={o.value}
                  type="button"
                  onClick={() => pickOutcome(o.value)}
                  className={cn(
                    "px-3 py-1.5 rounded-lg text-sm font-medium transition-colors",
                    outcome === o.value
                      ? "bg-brand-600 text-white"
                      : "bg-gray-800 text-gray-300 hover:bg-gray-700",
                  )}
                >
                  {o.label} <span className="text-xs opacity-70">@{o.odds?.toFixed(2)}</span>
                </button>
              ))}
              {options.length === 0 && (
                <p className="text-xs text-gray-500">Pas de cotes dispo sur ce match.</p>
              )}
            </div>
          </div>
        )}

        {/* 3. Cote / Montant / Bookmaker */}
        {outcome && (
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="text-xs text-gray-500 block mb-1">Ta cote</label>
              <input type="number" className="input" value={odds}
                onChange={(e) => setOdds(e.target.value)} min="1.01" step="0.01" />
            </div>
            <div>
              <label className="text-xs text-gray-500 block mb-1">Mise (€)</label>
              <input type="number" className="input" value={amount}
                onChange={(e) => setAmount(e.target.value)} min="1" step="1" />
            </div>
            <div>
              <label className="text-xs text-gray-500 block mb-1">Bookmaker</label>
              <input type="text" className="input" placeholder="Betclic..."
                value={bookmaker} onChange={(e) => setBookmaker(e.target.value)} />
            </div>
          </div>
        )}

        {error && <p className="text-sm text-edge-red">{error}</p>}
        {success && <p className="text-sm text-edge-green">{success}</p>}

        <button
          onClick={save}
          disabled={saving || !matchId || !outcome || !amount}
          className="btn-primary w-full"
        >
          {saving ? "Enregistrement..." : "Enregistrer ce pari"}
        </button>
        {user && (
          <p className="text-xs text-gray-500 text-center">
            Bankroll actuelle : {formatCurrency(user.bankroll)}
          </p>
        )}
      </div>

      {/* Paris en attente */}
      <div>
        <h2 className="font-semibold mb-3">Paris en attente ({bets.length})</h2>
        {bets.length === 0 ? (
          <div className="card text-center py-8 text-sm text-gray-500">
            Aucun pari en attente. Enregistre ton premier pari ci-dessus.
          </div>
        ) : (
          <div className="space-y-3">
            {bets.map((bet) => (
              <div key={bet.id} className="card">
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1">
                    <span className="text-xs px-2 py-0.5 rounded-full bg-brand-600/20 text-brand-300 font-semibold">
                      {STATUS_LABELS[bet.status] ?? bet.status} · {OUTCOME_LABELS[bet.outcome] ?? bet.outcome}
                    </span>
                    {bet.match && (
                      <p className="font-semibold mt-1">
                        {bet.match.home_team} <span className="text-gray-500 mx-1">vs</span> {bet.match.away_team}
                      </p>
                    )}
                    <p className="text-xs text-gray-500 mt-0.5">
                      {format(new Date(bet.placed_at), "d MMM yyyy HH:mm", { locale: fr })}
                      {bet.bookmaker ? ` · ${bet.bookmaker}` : ""}
                    </p>
                  </div>
                  <div className="text-right">
                    <p className="text-sm text-gray-400">Mise · Cote</p>
                    <p className="font-semibold">{formatCurrency(bet.amount)} @ {bet.odds.toFixed(2)}</p>
                  </div>
                </div>
                <div className="flex gap-2 mt-3 pt-3 border-t border-gray-800">
                  <button onClick={() => settle(bet.id, "WON")}
                    className="flex-1 py-1.5 rounded-lg text-sm font-medium bg-edge-green/10 text-edge-green hover:bg-edge-green/20">
                    Gagné
                  </button>
                  <button onClick={() => settle(bet.id, "LOST")}
                    className="flex-1 py-1.5 rounded-lg text-sm font-medium bg-edge-red/10 text-edge-red hover:bg-edge-red/20">
                    Perdu
                  </button>
                  <button onClick={() => settle(bet.id, "VOID")}
                    className="flex-1 py-1.5 rounded-lg text-sm font-medium bg-gray-800 text-gray-400 hover:bg-gray-700">
                    Annulé
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
