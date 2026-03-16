import React from 'react';
import { cn } from '@/lib/utils';
import {
  MonitorPlay,
  Presentation,
  Smartphone,
  Flame,
  Square,
  Zap,
} from 'lucide-react';

const PRESET_ICONS = {
  youtube_deep_dive: MonitorPlay,
  youtube_explainer: Presentation,
  youtube_short: Smartphone,
  tiktok: Flame,
  instagram_post: Square,
  doubt_clearer: Zap,
};

const PRESET_COLORS = {
  youtube_deep_dive: 'from-red-500/20 to-red-600/5 border-red-500/30 hover:border-red-400/50',
  youtube_explainer: 'from-blue-500/20 to-blue-600/5 border-blue-500/30 hover:border-blue-400/50',
  youtube_short: 'from-pink-500/20 to-pink-600/5 border-pink-500/30 hover:border-pink-400/50',
  tiktok: 'from-purple-500/20 to-purple-600/5 border-purple-500/30 hover:border-purple-400/50',
  instagram_post: 'from-amber-500/20 to-amber-600/5 border-amber-500/30 hover:border-amber-400/50',
  doubt_clearer: 'from-emerald-500/20 to-emerald-600/5 border-emerald-500/30 hover:border-emerald-400/50',
};

const ICON_COLORS = {
  youtube_deep_dive: 'text-red-400',
  youtube_explainer: 'text-blue-400',
  youtube_short: 'text-pink-400',
  tiktok: 'text-purple-400',
  instagram_post: 'text-amber-400',
  doubt_clearer: 'text-emerald-400',
};

export default function PresetChooser({ presets, onSelect }) {
  if (!presets || Object.keys(presets).length === 0) return null;

  return (
    <div className="flex flex-col items-center gap-5 p-4 sm:p-6 w-full max-w-2xl mx-auto animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="text-center space-y-1.5">
        <h2 className="text-lg sm:text-xl font-semibold text-foreground">
          Choose a video style
        </h2>
        <p className="text-xs sm:text-sm text-muted-foreground">
          Pick the format that fits your learning goal
        </p>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 w-full">
        {Object.entries(presets).map(([key, preset]) => {
          const Icon = PRESET_ICONS[key] || Presentation;
          const colors = PRESET_COLORS[key] || PRESET_COLORS.youtube_explainer;
          const iconColor = ICON_COLORS[key] || 'text-primary';

          return (
            <button
              key={key}
              onClick={() => onSelect(key)}
              className={cn(
                'group relative flex flex-col items-center gap-2.5 p-4 sm:p-5 rounded-2xl',
                'border bg-gradient-to-b transition-all duration-300',
                'hover:scale-[1.03] hover:shadow-lg active:scale-[0.98]',
                'cursor-pointer focus:outline-none focus:ring-2 focus:ring-primary/50',
                colors,
              )}
            >
              <div className={cn(
                'w-10 h-10 sm:w-12 sm:h-12 rounded-xl flex items-center justify-center',
                'bg-background/50 backdrop-blur-sm',
                'group-hover:bg-background/80 transition-colors',
              )}>
                <Icon className={cn('w-5 h-5 sm:w-6 sm:h-6', iconColor)} />
              </div>

              <div className="text-center space-y-0.5">
                <p className="text-xs sm:text-sm font-semibold text-foreground leading-tight">
                  {preset.name}
                </p>
                <p className="text-[10px] sm:text-xs text-muted-foreground leading-snug line-clamp-2">
                  {preset.description}
                </p>
              </div>

              <div className="text-[9px] sm:text-[10px] text-muted-foreground/60 font-mono">
                {preset.aspect_ratio} · {preset.width}×{preset.height}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
