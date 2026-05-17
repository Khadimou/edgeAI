"use client";

import { useState, useRef, useEffect } from "react";
import { MessageCircle, X, Send, Sparkles } from "lucide-react";
import { chatApi } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Message {
  role: "user" | "assistant";
  content: string;
  timestamp: number;
}

const STORAGE_KEY = "edgeai_chat_history";
const MAX_HISTORY = 50;

const SUGGESTED_QUESTIONS = [
  "C'est quoi le Kelly criterion ?",
  "Explique-moi l'Asian Handicap",
  "Pourquoi le CLV est important ?",
  "Comment lire l'edge ?",
];

export default function ChatBubble() {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [remaining, setRemaining] = useState<number | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Charge l'historique depuis localStorage au mount
  useEffect(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored) {
        const parsed = JSON.parse(stored) as Message[];
        setMessages(parsed.slice(-MAX_HISTORY));
      }
    } catch {
      // Ignore
    }
  }, []);

  // Persiste l'historique
  useEffect(() => {
    if (messages.length > 0) {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(messages.slice(-MAX_HISTORY)));
      } catch {
        // Ignore (quota dépassé ou navigation privée)
      }
    }
  }, [messages]);

  // Auto-scroll en bas + focus input quand ouvert
  useEffect(() => {
    if (open) {
      setTimeout(() => {
        scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
        inputRef.current?.focus();
      }, 50);
    }
  }, [open, messages.length]);

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || loading) return;
    setError(null);
    const newUserMsg: Message = { role: "user", content: trimmed, timestamp: Date.now() };
    const updated = [...messages, newUserMsg];
    setMessages(updated);
    setInput("");
    setLoading(true);

    try {
      const history = messages.slice(-10).map((m) => ({ role: m.role, content: m.content }));
      const r = await chatApi.message(trimmed, history);
      const reply: Message = {
        role: "assistant",
        content: r.data.reply,
        timestamp: Date.now(),
      };
      setMessages([...updated, reply]);
      setRemaining(r.data.rate_limit_remaining);
    } catch (e: unknown) {
      const err = e as { response?: { status?: number; data?: { detail?: string } } };
      if (err.response?.status === 429) {
        setError(err.response.data?.detail || "Limite atteinte. Réessaie dans 1h.");
      } else if (err.response?.status === 503) {
        setError("Chatbot non configuré (clé Anthropic manquante).");
      } else {
        setError("Erreur. Réessaie.");
      }
    } finally {
      setLoading(false);
    }
  }

  function clearHistory() {
    setMessages([]);
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      // Ignore
    }
  }

  return (
    <>
      {/* Bouton flottant */}
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(
          "fixed bottom-4 right-4 z-30 flex items-center justify-center",
          "w-12 h-12 sm:w-14 sm:h-14 rounded-full",
          "bg-brand-500 hover:bg-brand-400 text-white shadow-lg",
          "transition-transform hover:scale-110",
          open && "hidden",
        )}
        aria-label="Ouvrir l'assistant"
      >
        <MessageCircle className="w-5 h-5 sm:w-6 sm:h-6" />
      </button>

      {/* Drawer / Modal */}
      {open && (
        <>
          <div
            className="fixed inset-0 bg-black/40 z-40 sm:hidden"
            onClick={() => setOpen(false)}
          />
          <div
            className={cn(
              "fixed z-50 bg-gray-900 border border-gray-800 shadow-2xl",
              "flex flex-col",
              // Mobile : plein écran depuis le bas
              "inset-0 sm:inset-auto sm:bottom-4 sm:right-4 sm:left-auto sm:top-auto",
              "sm:w-[400px] sm:h-[600px] sm:max-h-[80vh] sm:rounded-xl",
            )}
          >
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800">
              <div className="flex items-center gap-2">
                <Sparkles className="w-4 h-4 text-brand-400" />
                <h3 className="font-semibold text-sm">Assistant edgeAI</h3>
                {remaining !== null && (
                  <span className="text-[10px] text-gray-500 ml-2">
                    {remaining} q. restantes/h
                  </span>
                )}
              </div>
              <div className="flex items-center gap-1">
                {messages.length > 0 && (
                  <button
                    onClick={clearHistory}
                    className="text-[11px] text-gray-500 hover:text-gray-300 px-2 py-1 mr-1"
                    title="Effacer l'historique"
                  >
                    Effacer
                  </button>
                )}
                <button
                  onClick={() => setOpen(false)}
                  className="p-1 text-gray-400 hover:text-white"
                  aria-label="Fermer"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>
            </div>

            {/* Messages */}
            <div
              ref={scrollRef}
              className="flex-1 overflow-y-auto px-4 py-3 space-y-3"
            >
              {messages.length === 0 ? (
                <div className="text-center py-6">
                  <Sparkles className="w-8 h-8 text-brand-400 mx-auto mb-3" />
                  <p className="text-sm text-gray-300 font-medium mb-1">
                    Salut ! Je suis ton assistant edgeAI.
                  </p>
                  <p className="text-xs text-gray-500 mb-4">
                    Pose-moi tes questions sur les termes techniques du betting.
                  </p>
                  <div className="space-y-1.5">
                    {SUGGESTED_QUESTIONS.map((q) => (
                      <button
                        key={q}
                        onClick={() => send(q)}
                        className="block w-full text-left text-xs bg-gray-800/50 hover:bg-gray-800 rounded-lg px-3 py-2 text-gray-300 transition"
                      >
                        {q}
                      </button>
                    ))}
                  </div>
                </div>
              ) : (
                messages.map((m, i) => (
                  <div
                    key={i}
                    className={cn(
                      "flex",
                      m.role === "user" ? "justify-end" : "justify-start",
                    )}
                  >
                    <div
                      className={cn(
                        "max-w-[85%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap",
                        m.role === "user"
                          ? "bg-brand-500/20 text-brand-100 border border-brand-500/30"
                          : "bg-gray-800 text-gray-100",
                      )}
                    >
                      {m.content}
                    </div>
                  </div>
                ))
              )}
              {loading && (
                <div className="flex justify-start">
                  <div className="bg-gray-800 rounded-lg px-3 py-2 text-sm text-gray-400">
                    <span className="inline-flex gap-1">
                      <span className="animate-pulse">●</span>
                      <span className="animate-pulse" style={{ animationDelay: "0.2s" }}>●</span>
                      <span className="animate-pulse" style={{ animationDelay: "0.4s" }}>●</span>
                    </span>
                  </div>
                </div>
              )}
              {error && (
                <div className="text-xs text-edge-red bg-red-500/10 border border-red-500/30 rounded-lg px-3 py-2">
                  {error}
                </div>
              )}
            </div>

            {/* Input */}
            <div className="border-t border-gray-800 p-3">
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  send(input);
                }}
                className="flex gap-2 items-end"
              >
                <textarea
                  ref={inputRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      send(input);
                    }
                  }}
                  rows={1}
                  placeholder="Ta question..."
                  className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:border-brand-500 resize-none max-h-32"
                  disabled={loading}
                />
                <button
                  type="submit"
                  disabled={loading || !input.trim()}
                  className="bg-brand-500 hover:bg-brand-400 disabled:opacity-30 disabled:cursor-not-allowed text-white rounded-lg p-2"
                  aria-label="Envoyer"
                >
                  <Send className="w-4 h-4" />
                </button>
              </form>
              <p className="text-[10px] text-gray-600 mt-2 text-center">
                Powered by Claude Haiku · Réponses indicatives, jamais des conseils financiers
              </p>
            </div>
          </div>
        </>
      )}
    </>
  );
}
