import React from 'react';
import { cn } from '@/lib/utils';
import { Loader2, Sparkles, Check } from 'lucide-react';

const STEP_LABELS = {
  queued: 'Starting up',
  processing_context: 'Researching your topic',
  planning_scenes: 'Planning the lesson',
  generating_audio: 'Generating narration',
  generating_scenes: 'Crafting video',
  stitching: 'Stitching final video',
  editing_code: 'Editing scene',
  completed: 'Done!',
};

export default function VideoGenerating({ serverStep, serverProgress, error }) {
  const label = STEP_LABELS[serverStep] || 'Processing';
  const progress = serverProgress ?? 0;
  const isDone = progress >= 100;

  return (
    <div className="flex flex-col items-center justify-center gap-5 p-6 sm:p-8 animate-in fade-in duration-500 w-full max-w-xs mx-auto">
      {/* Circular progress indicator */}
      <div className="relative w-24 h-24 sm:w-28 sm:h-28">
        <svg className="w-full h-full -rotate-90" viewBox="0 0 100 100">
          <circle
            cx="50" cy="50" r="42"
            fill="none"
            stroke="currentColor"
            strokeWidth="4"
            className="text-muted/20"
          />
          <circle
            cx="50" cy="50" r="42"
            fill="none"
            strokeWidth="4"
            strokeLinecap="round"
            className={cn(
              'transition-all duration-1000 ease-out',
              isDone ? 'text-emerald-500' : 'text-primary',
            )}
            stroke="currentColor"
            strokeDasharray={`${2 * Math.PI * 42}`}
            strokeDashoffset={`${2 * Math.PI * 42 * (1 - Math.max(0.02, progress / 100))}`}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          {isDone ? (
            <Check className="w-7 h-7 text-emerald-500" />
          ) : (
            <>
              <span className="text-xl sm:text-2xl font-semibold text-foreground tabular-nums">
                {Math.round(progress)}
              </span>
              <span className="text-[10px] text-muted-foreground -mt-0.5">percent</span>
            </>
          )}
        </div>
      </div>

      {/* Status */}
      {error ? (
        <div className="flex flex-col items-center gap-1.5 text-center">
          <p className="text-sm font-medium text-destructive">Generation failed</p>
          <p className="text-xs text-muted-foreground max-w-[220px]">{error}</p>
        </div>
      ) : (
        <div className="flex flex-col items-center gap-1 text-center">
          <div className="flex items-center gap-1.5">
            {!isDone && <Sparkles className="w-3.5 h-3.5 text-primary animate-pulse" />}
            <p className="text-sm font-medium text-foreground">{label}</p>
          </div>
          <p className="text-[11px] text-muted-foreground/50">Usually takes 1–2 minutes</p>
        </div>
      )}
    </div>
  );
}
