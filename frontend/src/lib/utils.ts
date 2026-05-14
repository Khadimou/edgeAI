import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatCurrency(amount: number, currency = "EUR"): string {
  return new Intl.NumberFormat("fr-FR", { style: "currency", currency }).format(amount);
}

export function formatPercent(value: number, decimals = 1): string {
  return `${(value * 100).toFixed(decimals)}%`;
}

export function formatOdds(odds: number): string {
  return odds.toFixed(2);
}

export function edgeColor(edge: number): string {
  if (edge >= 0.08) return "text-edge-green";
  if (edge >= 0.04) return "text-yellow-400";
  return "text-edge-red";
}

export function edgeBadgeVariant(edge: number): "default" | "secondary" | "destructive" {
  if (edge >= 0.08) return "default";
  if (edge >= 0.04) return "secondary";
  return "destructive";
}

export function outcomeLabel(outcome: string): string {
  const map: Record<string, string> = {
    HOME: "Domicile",
    DRAW: "Nul",
    AWAY: "Extérieur",
    OVER: "Plus de 2.5",
    UNDER: "Moins de 2.5",
  };
  return map[outcome] || outcome;
}

export function pickedTeamLabel(
  outcome: string,
  match?: { home_team?: string | null; away_team?: string | null } | null,
): string {
  if (outcome === "HOME") return match?.home_team || "Domicile";
  if (outcome === "AWAY") return match?.away_team || "Extérieur";
  if (outcome === "DRAW") return "Match nul";
  return outcomeLabel(outcome);
}

export function betStatusColor(status: string): string {
  const map: Record<string, string> = {
    WON: "text-edge-green",
    LOST: "text-edge-red",
    PENDING: "text-yellow-400",
    VOID: "text-gray-400",
    CASHOUT: "text-blue-400",
  };
  return map[status] || "text-gray-400";
}
