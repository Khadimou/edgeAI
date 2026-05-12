export interface Prediction {
  prob_home: number;
  prob_draw: number;
  prob_away: number;
  confidence: number;
  shap_values: Record<string, number> | null;
  model_version: string;
  computed_at: string;
}

export interface MatchSummary {
  id: string;
  external_id: string;
  league: string;
  season: string;
  home_team: string;
  away_team: string;
  match_date: string;
  status: string;
  home_score: number | null;
  away_score: number | null;
  home_odds: number | null;
  draw_odds: number | null;
  away_odds: number | null;
  venue: string | null;
  prediction: Prediction | null;
}

export interface Recommendation {
  id: string;
  match_id: string;
  home_team: string;
  away_team: string;
  league: string;
  match_date: string;
  outcome: string;
  edge: number;
  kelly_stake: number | null;
  recommended_amount: number | null;
  odds: number;
  prob_home: number;
  prob_draw: number;
  prob_away: number;
  confidence: number;
  strategy: string | null;
  model_version: string;
}

export interface BetMatch {
  id: string;
  home_team: string;
  away_team: string;
  league: string;
  match_date: string;
  status: string;
}

export interface Bet {
  id: string;
  match_id: string;
  recommendation_id: string | null;
  outcome: string;
  amount: number;
  odds: number;
  status: string;
  profit_loss: number | null;
  bookmaker: string | null;
  notes: string | null;
  placed_at: string;
  settled_at: string | null;
  match: BetMatch | null;
}

export interface BankrollEntry {
  id: string;
  amount: number;
  balance: number;
  event_type: string;
  reference_id: string | null;
  timestamp: string;
}

export interface BankrollStats {
  current_balance: number;
  total_deposited: number;
  total_profit_loss: number;
  roi_percent: number;
  history: BankrollEntry[];
}

export interface PerformanceStats {
  total_bets: number;
  won: number;
  lost: number;
  pending: number;
  win_rate: number;
  roi_percent: number;
  total_profit_loss: number;
  avg_odds: number;
  expected_value_realized: number;
  best_streak: number;
  current_streak: number;
  by_league: Record<string, { bets: number; won: number; pnl: number }>;
  by_outcome: Record<string, { bets: number; won: number; pnl: number }>;
  monthly_pnl: Array<{ month: string; pnl: number }>;
}
