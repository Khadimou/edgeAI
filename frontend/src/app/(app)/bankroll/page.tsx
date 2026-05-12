"use client";

import { useQuery } from "@tanstack/react-query";
import { format } from "date-fns";
import { fr } from "date-fns/locale";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { bankrollApi } from "@/lib/api";
import { formatCurrency, cn } from "@/lib/utils";
import type { BankrollStats, BankrollEntry } from "@/types/api";

const EVENT_LABELS: Record<string, string> = {
  DEPOSIT: "Dépôt",
  WITHDRAWAL: "Retrait",
  BET_PLACED: "Pari placé",
  BET_WON: "Pari gagné",
  BET_LOST: "Pari perdu",
  BET_VOID: "Pari annulé",
  SUBSCRIPTION_CREDIT: "Abonnement",
  REFERRAL_BONUS: "Bonus parrainage",
};

const EVENT_COLORS: Record<string, string> = {
  BET_WON: "text-edge-green",
  DEPOSIT: "text-edge-green",
  REFERRAL_BONUS: "text-edge-green",
  BET_LOST: "text-edge-red",
  WITHDRAWAL: "text-edge-red",
  BET_PLACED: "text-yellow-400",
  BET_VOID: "text-gray-400",
  SUBSCRIPTION_CREDIT: "text-gray-400",
};

export default function BankrollPage() {
  const { data, isLoading } = useQuery<BankrollStats>({
    queryKey: ["bankroll"],
    queryFn: () => bankrollApi.history().then((r) => r.data),
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin w-8 h-8 border-2 border-brand-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  const chartData = data?.history.map((h) => ({
    date: format(new Date(h.timestamp), "d MMM", { locale: fr }),
    balance: h.balance,
  })) ?? [];

  const roiColor = (data?.roi_percent ?? 0) >= 0 ? "text-edge-green" : "text-edge-red";

  return (
    <div className="space-y-8">
      <h1 className="text-2xl font-bold">Bankroll</h1>

      {/* KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="stat-card">
          <div className="stat-label">Balance actuelle</div>
          <div className="stat-value text-brand-400">
            {formatCurrency(data?.current_balance ?? 0)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total déposé</div>
          <div className="stat-value">{formatCurrency(data?.total_deposited ?? 0)}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">P&L total</div>
          <div className={cn("stat-value", roiColor)}>
            {(data?.total_profit_loss ?? 0) >= 0 ? "+" : ""}
            {formatCurrency(data?.total_profit_loss ?? 0)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">ROI</div>
          <div className={cn("stat-value", roiColor)}>
            {(data?.roi_percent ?? 0) >= 0 ? "+" : ""}
            {(data?.roi_percent ?? 0).toFixed(1)}%
          </div>
        </div>
      </div>

      {/* Graphique */}
      {chartData.length > 1 && (
        <div className="card">
          <h2 className="font-semibold mb-5">Évolution de la bankroll</h2>
          <ResponsiveContainer width="100%" height={260}>
            <AreaChart data={chartData}>
              <defs>
                <linearGradient id="balGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#0ea5e9" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#0ea5e9" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
              <XAxis dataKey="date" tick={{ fill: "#6b7280", fontSize: 12 }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fill: "#6b7280", fontSize: 12 }} axisLine={false} tickLine={false} tickFormatter={(v: number) => `${v}€`} />
              <Tooltip
                contentStyle={{ backgroundColor: "#111827", border: "1px solid #374151", borderRadius: 8 }}
                labelStyle={{ color: "#9ca3af" }}
                formatter={(v: number) => [formatCurrency(v), "Balance"]}
              />
              <Area type="monotone" dataKey="balance" stroke="#0ea5e9" fill="url(#balGrad)" strokeWidth={2} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Historique */}
      <div className="card">
        <h2 className="font-semibold mb-4">Historique des mouvements</h2>
        {!data?.history.length ? (
          <p className="text-gray-500 text-sm text-center py-8">
            Aucun mouvement. Déposez une bankroll dans vos paramètres.
          </p>
        ) : (
          <div className="space-y-0 divide-y divide-gray-800">
            {[...data.history].reverse().map((h: BankrollEntry) => (
              <div key={h.id} className="flex items-center justify-between py-3">
                <div>
                  <p className="text-sm font-medium">{EVENT_LABELS[h.event_type] ?? h.event_type}</p>
                  <p className="text-xs text-gray-500">
                    {format(new Date(h.timestamp), "d MMM yyyy HH:mm", { locale: fr })}
                  </p>
                </div>
                <div className="text-right">
                  <p className={cn("font-semibold text-sm", EVENT_COLORS[h.event_type] ?? "text-gray-400")}>
                    {h.amount > 0 ? "+" : ""}{formatCurrency(h.amount)}
                  </p>
                  <p className="text-xs text-gray-500">{formatCurrency(h.balance)}</p>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
