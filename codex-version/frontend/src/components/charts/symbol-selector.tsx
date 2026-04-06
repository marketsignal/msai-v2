"use client";

type SymbolSelectorProps = {
  symbols: Record<string, string[]>;
  value: string;
  onChange: (symbol: string) => void;
};

export function SymbolSelector({ symbols, value, onChange }: SymbolSelectorProps) {
  return (
    <label className="block space-y-1 text-sm text-zinc-300">
      Symbol
      <select
        className="w-full rounded-md border border-white/10 bg-black/40 p-2"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {Object.entries(symbols).map(([asset, items]) => (
          <optgroup key={asset} label={asset.toUpperCase()}>
            {items.map((symbol) => (
              <option key={symbol} value={symbol}>
                {symbol}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
    </label>
  );
}
