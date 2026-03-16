import React, { useState, useRef } from 'react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import {
  Upload,
  FileText,
  Image,
  Link2,
  X,
  Plus,
  ArrowRight,
} from 'lucide-react';

export default function ResourceUpload({ onSubmit, onSkip }) {
  const [files, setFiles] = useState([]);
  const [urlInput, setUrlInput] = useState('');
  const [urls, setUrls] = useState([]);
  const fileInputRef = useRef(null);

  function handleFiles(e) {
    const added = Array.from(e.target.files || []);
    setFiles((prev) => [...prev, ...added]);
    e.target.value = '';
  }

  function removeFile(idx) {
    setFiles((prev) => prev.filter((_, i) => i !== idx));
  }

  function addUrl() {
    const trimmed = urlInput.trim();
    if (trimmed && !urls.includes(trimmed)) {
      setUrls((prev) => [...prev, trimmed]);
      setUrlInput('');
    }
  }

  function removeUrl(idx) {
    setUrls((prev) => prev.filter((_, i) => i !== idx));
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      addUrl();
    }
  }

  const hasResources = files.length > 0 || urls.length > 0;

  return (
    <div className="flex flex-col items-center gap-5 p-4 sm:p-6 w-full max-w-lg mx-auto animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="text-center space-y-1.5">
        <h2 className="text-lg sm:text-xl font-semibold text-foreground">
          Add learning resources
        </h2>
        <p className="text-xs sm:text-sm text-muted-foreground">
          Upload PDFs, handwritten notes, or paste reference links
        </p>
      </div>

      {/* Drop zone */}
      <button
        onClick={() => fileInputRef.current?.click()}
        className={cn(
          'w-full border-2 border-dashed rounded-2xl p-6 sm:p-8',
          'flex flex-col items-center gap-3 transition-all duration-200',
          'hover:border-primary/50 hover:bg-primary/5 cursor-pointer',
          'border-border/40 bg-background/50',
        )}
      >
        <div className="w-12 h-12 rounded-xl bg-primary/10 flex items-center justify-center">
          <Upload className="w-5 h-5 text-primary" />
        </div>
        <div className="text-center">
          <p className="text-sm font-medium text-foreground">Upload files</p>
          <p className="text-xs text-muted-foreground mt-0.5">
            PDFs, images, handwritten notes
          </p>
        </div>
      </button>
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept=".pdf,.png,.jpg,.jpeg,.webp,.gif"
        className="hidden"
        onChange={handleFiles}
      />

      {/* File list */}
      {files.length > 0 && (
        <div className="w-full space-y-1.5">
          {files.map((f, i) => (
            <div
              key={`${f.name}-${i}`}
              className="flex items-center gap-2.5 px-3 py-2 rounded-lg bg-muted/30 border border-border/30"
            >
              {f.type.startsWith('image/') ? (
                <Image className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
              ) : (
                <FileText className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
              )}
              <span className="text-xs text-foreground truncate flex-1">{f.name}</span>
              <button onClick={() => removeFile(i)} className="text-muted-foreground hover:text-destructive">
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* URL input */}
      <div className="w-full space-y-2">
        <div className="flex gap-2">
          <div className="flex-1 flex items-center gap-2 px-3 py-2 rounded-lg border border-border/40 bg-background/50 focus-within:border-primary/50 transition-colors">
            <Link2 className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
            <input
              type="url"
              value={urlInput}
              onChange={(e) => setUrlInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Paste a reference URL..."
              className="flex-1 bg-transparent text-xs text-foreground outline-none placeholder:text-muted-foreground/50"
            />
          </div>
          <Button
            size="sm"
            variant="outline"
            onClick={addUrl}
            disabled={!urlInput.trim()}
            className="rounded-lg px-2.5"
          >
            <Plus className="w-3.5 h-3.5" />
          </Button>
        </div>

        {urls.length > 0 && (
          <div className="space-y-1.5">
            {urls.map((url, i) => (
              <div
                key={`url-${i}`}
                className="flex items-center gap-2.5 px-3 py-2 rounded-lg bg-muted/30 border border-border/30"
              >
                <Link2 className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                <span className="text-xs text-foreground truncate flex-1">{url}</span>
                <button onClick={() => removeUrl(i)} className="text-muted-foreground hover:text-destructive">
                  <X className="w-3.5 h-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="flex items-center gap-3 w-full">
        <Button
          variant="ghost"
          onClick={onSkip}
          className="flex-1 rounded-xl text-xs sm:text-sm"
        >
          Skip
        </Button>
        <Button
          onClick={() => onSubmit({ files, urls })}
          disabled={!hasResources}
          className="flex-1 rounded-xl text-xs sm:text-sm gap-1.5"
        >
          Continue
          <ArrowRight className="w-3.5 h-3.5" />
        </Button>
      </div>
    </div>
  );
}
