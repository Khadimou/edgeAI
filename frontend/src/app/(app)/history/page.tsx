"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { format } from "date-fns";
import { fr } from "date-fns/locale";
import { betsApi } from "@/lib/api";
import { formatCurrency, outcomeLabel, betStatusColor, cn } from "@/lib/utils";
import type { Bet } from "@/types/api";

const STATUS_LABELS: Record<string, string> = {
  PENDING: "En attente",
  WON: "Gagné",
  LOST: "Perdu",
  VOID: "Annulé",
  CASHOUT: "Cash-out",
};

const FILTERS = [
  { value: "", label: "Tous" },
  { value: "PENDING", label: "En attente" },
  { value: "WON", label: "Gagnés" },
  { value: "LOST", label: "Perdus" },
];

export default function HistoryPage() {
  const [statusFilter, setStatusFilter] = useState("");

  const { data: bets = [], isLoading, refetch } = useQuery<Bet[]>({
    queryKey: ["bets", statusFilter],
    queryFn: () => betsApi.list(statusFilter || undefined).then((r) => r.data),
  });

  const totalPnl = bets.reduce((s, b) => s + (b.profit_loss ?? 0), 0);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Historique des paris</h1>
        <div className="text-right">
          <p className="text-sm text-gray-400">{bets.length} paris</p>
          <p className={cn("font-semibold", totalPnl >= 0 ? "text-edge-green" : "text-edge-red")}>
            P&L: {totalPnl >= 0 ? "+" : ""}{formatCurrency(totalPnl)}
          </p>
        </div>
      </div>

      {/* Filtres */}
      <div className="flex gap-2">
        {FILTERS.map((f) => (
          <button
            key={f.value}
            onClick={() => setStatusFilter(f.value)}
            className={cn(
              "px-3 py-1.5 rounded-lg text-sm font-medium transition-colors",
              statusFilter === f.value
                ? "bg-brand-600 text-white"
                : "bg-gray-800 text-gray-400 hover:bg-gray-700"
            )}
          >
            {f.label}
          </button>
        ))}
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center h-40">
          <div className="animate-spin w-8 h-8 border-2 border-brand-500 border-t-transparent rounded-full" />
        </div>
      ) : bets.length === 0 ? (
        <div className="card text-center py-14">
          <p className="text-gray-400">Aucun pari trouvé.</p>
          <p className="text-sm text-gray-500 mt-1">
            Analysez un match et enregistrez votre premier pari.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {bets.map((bet) => (
            <BetRow key={bet.id} bet={bet} onUpdate={refetch} />
          ))}
        </div>
      )}
    </div>
  );
}

function BetRow({ bet, onUpdate }: { bet: Bet; onUpdate: () => void }) {
  const [updating, setUpdating] = useState(false);

  async function settle(status: string) {
    setUpdating(true);
    try {
      await betsApi.updateResult(bet.id, { status });
      onUpdate();
    } finally {
      setUpdating(false);
    }
  }

  return (
    <div className="card">
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-1">
            <span className={cn(
              "text-xs font-semibold px-2 py-0.5 rounded-full bg-gray-800",
              betStatusColor(bet.status)
            )}>
              {STATUS_LABELS[bet.status] ?? bet.status}
            </span>
            <span className="text-sm text-gray-400">{outcomeLabel(bet.outcome)}</span>
          </div>
          {bet.match && (
            <p className="font-semibold">
              {bet.match.home_team} vs {bet.match.away_team}
            </p>
          )}
          <p className="text-xs text-gray-500 mt-0.5">
            {format(new Date(bet.placed_at), "d MMM yyyy HH:mm", { locale: fr })}
            {bet.bookmaker ? ` · ${bet.bookmaker}` : ""}
          </p>
        </div>
        <div className="text-right">
          <p className="text-sm text-gray-400">Mise · Cote</p>
          <p className="font-semibold">
            {formatCurrency(bet.amount)} @ {bet.odds.toFixed(2)}
          </p>
          {bet.profit_loss != null && bet.status !== "PENDING" && (
            <p className={cn("font-bold mt-1", bet.profit_loss >= 0 ? "text-edge-green" : "text-edge-red")}>
              {bet.profit_loss >= 0 ? "+" : ""}{formatCurrency(bet.profit_loss)}
            </p>
          )}
        </div>
      </div>

      {bet.status === "PENDING" && (
        <div className="flex gap-2 mt-3 pt-3 border-t border-gray-800">
          <button
            onClick={() => settle("WON")}
            disabled={updating}
            className="flex-1 py-1.5 rounded-lg text-sm font-medium bg-edge-green/10 text-edge-green hover:bg-edge-green/20 transition-colors disabled:opacity-50"
          >
            Gagné
          </button>
          <button
            onClick={() => settle("LOST")}
            disabled={updating}
            className="flex-1 py-1.5 rounded-lg text-sm font-medium bg-edge-red/10 text-edge-red hover:bg-edge-red/20 transition-colors disabled:opacity-50"
          >
            Perdu
          </button>
          <button
            onClick={() => settle("VOID")}
            disabled={updating}
            className="flex-1 py-1.5 rounded-lg text-sm font-medium bg-gray-800 text-gray-400 hover:bg-gray-700 transition-colors disabled:opacity-50"
          >
            Annulé
          </button>
        </div>
      )}
    </div>
  );
}
