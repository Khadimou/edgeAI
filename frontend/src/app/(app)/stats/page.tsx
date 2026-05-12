"use client";

import { useQuery } from "@tanstack/react-query";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine,
} from "recharts";
import { statsApi } from "@/lib/api";
import { formatCurrency, formatPercent, cn } from "@/lib/utils";
import { Target, AlertCircle } from "lucide-react";
import type { PerformanceStats } from "@/types/api";

export default function StatsPage() {
  const { data: stats, isLoading } = useQuery<PerformanceStats>({
    queryKey: ["stats"],
    queryFn: () => statsApi.performance().then((r) => r.data),
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin w-8 h-8 border-2 border-brand-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (!stats || stats.total_bets === 0) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold">Statistiques</h1>
        <div className="card text-center py-14">
          <Target className="w-12 h-12 text-gray-600 mx-auto mb-3" />
          <p className="text-gray-400">Pas encore de données.</p>
          <p className="text-sm text-gray-500 mt-1">Enregistrez vos paris pour voir vos stats.</p>
        </div>
      </div>
    );
  }

  const roiColor = stats.roi_percent >= 0 ? "text-edge-green" : "text-edge-red";

  return (
    <div className="space-y-8">
      <h1 className="text-2xl font-bold">Statistiques de performance</h1>

      {/* KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="stat-card">
          <div className="stat-label">Win rate</div>
          <div className={cn("stat-value", stats.win_rate >= 0.54 ? "text-edge-green" : "text-yellow-400")}>
            {formatPercent(stats.win_rate)}
          </div>
          <div className="text-xs text-gray-500 mt-1">{stats.won}W / {stats.lost}L / {stats.pending}P</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">ROI</div>
          <div className={cn("stat-value", roiColor)}>
            {stats.roi_percent >= 0 ? "+" : ""}{stats.roi_percent.toFixed(1)}%
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">P&L total</div>
          <div className={cn("stat-value", stats.total_profit_loss >= 0 ? "text-edge-green" : "text-edge-red")}>
            {formatCurrency(stats.total_profit_loss)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Série actuelle</div>
          <div className={cn("stat-value", stats.current_streak > 0 ? "text-edge-green" : "text-edge-red")}>
            {stats.current_streak > 0 ? "+" : ""}{stats.current_streak}
          </div>
          <div className="text-xs text-gray-500 mt-1">Meilleure : {stats.best_streak}</div>
        </div>
      </div>

      {/* P&L mensuel */}
      {stats.monthly_pnl?.length > 0 && (
        <div className="card">
          <h2 className="font-semibold mb-5">P&L mensuel</h2>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={stats.monthly_pnl}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
              <XAxis dataKey="month" tick={{ fill: "#6b7280", fontSize: 12 }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fill: "#6b7280", fontSize: 12 }} axisLine={false} tickLine={false} tickFormatter={(v) => `${v}€`} />
              <Tooltip
                contentStyle={{ backgroundColor: "#111827", border: "1px solid #374151", borderRadius: 8 }}
                formatter={(v: number) => [formatCurrency(v), "P&L"]}
              />
              <ReferenceLine y={0} stroke="#374151" />
              <Bar
                dataKey="pnl"
                radius={[4, 4, 0, 0]}
                fill="#0ea5e9"
              />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Par ligue */}
      {Object.keys(stats.by_league || {}).length > 0 && (
        <div className="card">
          <h2 className="font-semibold mb-4">Performance par ligue</h2>
          <div className="space-y-0 divide-y divide-gray-800">
            {Object.entries(stats.by_league)
              .sort(([, a], [, b]) => b.pnl - a.pnl)
              .map(([league, data]) => (
                <div key={league} className="flex items-center justify-between py-3">
                  <div>
                    <p className="font-medium text-sm">{league}</p>
                    <p className="text-xs text-gray-500">{data.bets} paris · {data.won}W</p>
                  </div>
                  <div className="text-right">
                    <p className={cn("font-semibold text-sm", data.pnl >= 0 ? "text-edge-green" : "text-edge-red")}>
                      {data.pnl >= 0 ? "+" : ""}{formatCurrency(data.pnl)}
                    </p>
                    <p className="text-xs text-gray-500">
                      {data.bets > 0 ? formatPercent(data.won / data.bets) : "—"} win rate
                    </p>
                  </div>
                </div>
              ))}
          </div>
        </div>
      )}

      <div className="card border-yellow-500/20 bg-yellow-500/5">
        <div className="flex gap-3">
          <AlertCircle className="w-5 h-5 text-yellow-400 flex-shrink-0 mt-0.5" />
          <div className="text-sm text-yellow-400/80">
            <strong className="text-yellow-400">Rappel :</strong> Les résultats passés ne garantissent pas
            les performances futures. La variance à court terme est normale — l&apos;edge se matérialise sur
            le long terme (minimum 500 paris recommandés pour une évaluation statistiquement significative).
          </div>
        </div>
      </div>
    </div>
  );
}
