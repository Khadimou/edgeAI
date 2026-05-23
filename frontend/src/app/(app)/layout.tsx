"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import Link from "next/link";
import {
  LayoutDashboard,
  Target,
  History,
  BarChart3,
  Wallet,
  Settings,
  LogOut,
  Flame,
  Brain,
  FlaskConical,
  Activity,
  ServerCog,
  NotebookPen,
  Menu,
  X,
} from "lucide-react";
import { useAuthStore } from "@/store/auth";
import { cn } from "@/lib/utils";
import ChatBubble from "@/components/ChatBubble";

const NAV_ITEMS = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { href: "/today", label: "Aujourd'hui", icon: Flame, highlight: true },
  { href: "/plan", label: "Mon Plan", icon: Target },
  { href: "/bankroll", label: "Bankroll", icon: Wallet },
  { href: "/journal", label: "Mes paris", icon: NotebookPen },
  { href: "/history", label: "Historique", icon: History },
  { href: "/stats", label: "Stats", icon: BarChart3 },
  { href: "/model", label: "Modèle IA", icon: Brain },
  { href: "/tracking", label: "Live tracking", icon: Activity },
  { href: "/backtest", label: "Backtest", icon: FlaskConical },
  { href: "/admin", label: "Admin", icon: ServerCog },
  { href: "/settings", label: "Paramètres", icon: Settings },
];

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const { user, logout, isAuthenticated } = useAuthStore();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  useEffect(() => {
    if (!isAuthenticated()) {
      router.push("/login");
    }
  }, [isAuthenticated, router]);

  // Ferme le drawer mobile à chaque changement de route
  useEffect(() => {
    setMobileNavOpen(false);
  }, [pathname]);

  // Empêche le scroll body quand drawer ouvert
  useEffect(() => {
    if (mobileNavOpen) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => { document.body.style.overflow = ""; };
  }, [mobileNavOpen]);

  if (!user) return null;

  const sidebarContent = (
    <>
      <div className="p-5 border-b border-gray-800">
        <Link href="/dashboard" className="text-xl font-bold text-brand-500">edgeAI</Link>
        <div className="mt-2 flex items-center gap-2">
          <span className={cn(
            "text-xs px-2 py-0.5 rounded-full font-semibold",
            user.plan === "ELITE" ? "bg-purple-500/20 text-purple-400" :
            user.plan === "PRO" ? "bg-brand-500/20 text-brand-400" :
            "bg-gray-700 text-gray-400"
          )}>
            {user.plan}
          </span>
        </div>
      </div>

      <nav className="flex-1 p-3 space-y-1 overflow-y-auto">
        {NAV_ITEMS.map((item) => {
          const isActive = pathname === item.href || pathname.startsWith(item.href + "/");
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors",
                isActive
                  ? item.highlight
                    ? "bg-orange-500/20 text-orange-400"
                    : "bg-brand-600/20 text-brand-400"
                  : item.highlight
                    ? "text-orange-400 hover:bg-orange-500/10"
                    : "text-gray-400 hover:text-gray-100 hover:bg-gray-800"
              )}
            >
              <item.icon className="w-4 h-4" />
              {item.label}
            </Link>
          );
        })}
      </nav>

      <div className="p-3 border-t border-gray-800">
        <div className="px-3 py-2 mb-2">
          <p className="text-sm font-medium truncate">{user.name || user.email}</p>
          <p className="text-xs text-gray-500">
            Bankroll: <span className="text-gray-300">{user.bankroll.toFixed(0)}€</span>
          </p>
        </div>
        <button
          onClick={() => { logout(); router.push("/"); }}
          className="flex items-center gap-3 w-full px-3 py-2 rounded-lg text-sm text-gray-400 hover:text-gray-100 hover:bg-gray-800 transition-colors"
        >
          <LogOut className="w-4 h-4" />
          Se déconnecter
        </button>
      </div>
    </>
  );

  return (
    <div className="min-h-screen lg:flex">
      {/* Topbar mobile (visible < lg) */}
      <header className="lg:hidden sticky top-0 z-30 flex items-center justify-between bg-gray-900 border-b border-gray-800 px-4 py-3">
        <button
          onClick={() => setMobileNavOpen(true)}
          className="p-2 -m-2 text-gray-300 hover:text-white"
          aria-label="Ouvrir le menu"
        >
          <Menu className="w-5 h-5" />
        </button>
        <Link href="/dashboard" className="text-lg font-bold text-brand-500">edgeAI</Link>
        <Link href="/settings" className="text-xs text-gray-400">
          {user.bankroll.toFixed(0)}€
        </Link>
      </header>

      {/* Sidebar desktop (>= lg) */}
      <aside className="hidden lg:flex w-56 bg-gray-900 border-r border-gray-800 flex-col fixed h-full z-10">
        {sidebarContent}
      </aside>

      {/* Drawer mobile (overlay) */}
      {mobileNavOpen && (
        <>
          <div
            className="lg:hidden fixed inset-0 bg-black/70 z-40"
            onClick={() => setMobileNavOpen(false)}
            aria-hidden="true"
          />
          <aside className="lg:hidden fixed top-0 left-0 bottom-0 w-64 max-w-[80vw] bg-gray-900 border-r border-gray-800 flex flex-col z-50 animate-slide-in">
            <button
              onClick={() => setMobileNavOpen(false)}
              className="absolute top-3 right-3 p-2 text-gray-400 hover:text-white"
              aria-label="Fermer le menu"
            >
              <X className="w-5 h-5" />
            </button>
            {sidebarContent}
          </aside>
        </>
      )}

      {/* Main */}
      <main className="flex-1 lg:ml-56 min-h-screen">
        <div className="max-w-6xl mx-auto px-4 py-4 sm:px-6 sm:py-8">
          {children}
        </div>
      </main>

      {/* Chatbot pédagogique (toutes pages app) */}
      <ChatBubble />
    </div>
  );
}
