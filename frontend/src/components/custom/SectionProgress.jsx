import React from 'react';
import { cn } from '@/lib/utils';
import { CheckCircle2, Circle, PlayCircle } from 'lucide-react';

export default function SectionProgress({ sections, currentTime = 0, videoState }) {
  if (!sections || sections.length === 0) return null;

  const isPlaying = videoState === 1;
  const isPaused = videoState === 2;
  const isEnded = videoState === 3;

  return (
    <div className="flex flex-col gap-0.5 py-3">
      <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider px-4 mb-2">
        Lesson Progress
      </h3>
      {sections.map((section, idx) => {
        const isCompleted = currentTime >= section.endTime;
        const isCurrent = currentTime >= section.startTime && currentTime < section.endTime;

        let progress = 0;
        if (isCompleted || isEnded) {
          progress = 100;
        } else if (isCurrent) {
          const elapsed = currentTime - section.startTime;
          const duration = section.endTime - section.startTime;
          progress = duration > 0 ? Math.min(100, (elapsed / duration) * 100) : 0;
        }

        const StatusIcon = isCompleted
          ? CheckCircle2
          : isCurrent
            ? (isPlaying || isPaused) ? PlayCircle : Circle
            : Circle;

        return (
          <div
            key={section.id}
            className={cn(
              'relative px-4 py-2 flex items-start gap-3 transition-all duration-300',
              isCurrent && 'bg-primary/5',
            )}
          >
            {idx < sections.length - 1 && (
              <div
                className={cn(
                  'absolute left-[1.65rem] top-[1.85rem] w-[1.5px] bottom-0',
                  isCompleted ? 'bg-primary/40' : 'bg-border',
                )}
              />
            )}

            <div className="relative z-10 mt-0.5 shrink-0">
              <StatusIcon
                className={cn(
                  'w-3.5 h-3.5 transition-colors duration-300',
                  isCompleted
                    ? 'text-emerald-400'
                    : isCurrent
                      ? 'text-primary animate-pulse'
                      : 'text-muted-foreground/40',
                )}
              />
            </div>

            <div className="flex-1 min-w-0">
              <p
                className={cn(
                  'text-xs sm:text-sm font-medium leading-tight transition-colors duration-300',
                  isCompleted
                    ? 'text-muted-foreground'
                    : isCurrent
                      ? 'text-foreground'
                      : 'text-muted-foreground/60',
                )}
              >
                {section.title}
              </p>

              {(isCurrent || isCompleted) && (
                <div className="mt-1.5 h-1 rounded-full bg-border overflow-hidden">
                  <div
                    className={cn(
                      'h-full rounded-full transition-all duration-500 ease-out',
                      isCompleted ? 'bg-emerald-400/60' : 'bg-primary/70',
                    )}
                    style={{ width: `${progress}%` }}
                  />
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
