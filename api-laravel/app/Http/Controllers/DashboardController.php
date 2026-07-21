<?php

namespace App\Http\Controllers;

use Illuminate\Http\Request;
use App\Models\ActiveSymbol;
use App\Models\MacroState;
use App\Models\Position;
use Illuminate\Support\Facades\Http;
use App\Models\BotLog;

class DashboardController extends Controller
{
    public function logs()
    {
        $logs = BotLog::orderBy('created_at', 'desc')->take(100)->get();
        return response()->json(array_reverse($logs->toArray()));
    }

    public function getDashboardMetrics()
    {
        // 1. Total PNL from CLOSED trades
        $totalPnl = Position::whereIn('decision', ['CLOSE_LONG', 'CLOSE_SHORT'])
                            ->whereNotNull('pnl_usd')
                            ->sum('pnl_usd'); 
        
        // 2. Win Rate from CLOSED trades
        $winningTrades = Position::whereIn('decision', ['CLOSE_LONG', 'CLOSE_SHORT'])
                                 ->where('pnl_usd', '>', 0)
                                 ->count();
        $totalTrades = Position::whereIn('decision', ['CLOSE_LONG', 'CLOSE_SHORT'])
                               ->whereNotNull('pnl_usd')
                               ->count() ?: 1;
        $winRate = round(($winningTrades / $totalTrades) * 100, 2);

        // 3. Wallet Balance from Binance Testnet
        $apiKey = env('BINANCE_API_KEY');
        $apiSecret = env('BINANCE_API_SECRET');
        $walletBalance = 0.00;

        if ($apiKey && $apiSecret) {
            $timestamp = round(microtime(true) * 1000);
            $queryString = "timestamp=" . $timestamp;
            $signature = hash_hmac('sha256', $queryString, $apiSecret);

            try {
                $response = Http::withHeaders([
                    'X-MBX-APIKEY' => $apiKey
                ])->get("https://testnet.binancefuture.com/fapi/v2/account?{$queryString}&signature={$signature}");

                if ($response->successful()) {
                    $account = $response->json();
                    if (isset($account['totalWalletBalance'])) {
                        $walletBalance = (float) $account['totalWalletBalance'];
                    }
                }
            } catch (\Exception $e) {
                $walletBalance = 0.00;
            }
        }

        // 4. Active Positions
        $positions = Position::where('asset_balance', '>', 0)
            ->whereIn('id', function($query) {
                $query->selectRaw('MAX(id)')
                      ->from('positions')
                      ->groupBy('symbol');
            })
            ->get();
            
        // Map to exact JSON structure requested
        $mappedPositions = $positions->map(function ($pos) {
            $unrealized = 0;
            if ($pos->position_direction === 'LONG') {
                $unrealized = ($pos->current_price - $pos->average_entry_price) * $pos->asset_balance;
            } else if ($pos->position_direction === 'SHORT') {
                $unrealized = ($pos->average_entry_price - $pos->current_price) * $pos->asset_balance;
            }

            return [
                'symbol' => $pos->symbol,
                'direction' => $pos->position_direction,
                'entry_price' => number_format((float)$pos->average_entry_price, 2, '.', ''),
                'current_price' => number_format((float)$pos->current_price, 2, '.', ''),
                'unrealized_pnl' => number_format((float)$unrealized, 2, '.', ''),
                'entry_reason' => $pos->entry_reason
            ];
        });

        return response()->json([
            'wallet_balance' => number_format($walletBalance, 2, '.', ''),
            'total_pnl' => number_format($totalPnl, 2, '.', ''),
            'win_rate' => $winRate,
            'active_positions' => $mappedPositions
        ]);
    }

    public function macroTrends()
    {
        // Only return macro trends for symbols currently active in the scanner
        $activeSymbols = ActiveSymbol::where('is_active', true)->pluck('symbol');

        $trends = MacroState::whereIn('symbol', $activeSymbols)->get();

        return response()->json($trends);
    }



    public function symbols()
    {
        return response()->json(ActiveSymbol::where('is_active', true)->get());
    }

    public function addSymbol(Request $request)
    {
        $validated = $request->validate([
            'symbol' => 'required|string|unique:active_symbols,symbol'
        ]);

        $symbol = ActiveSymbol::create([
            'symbol' => strtoupper($validated['symbol']),
            'is_active' => true
        ]);

        return response()->json(['message' => 'Symbol added successfully', 'data' => $symbol]);
    }

    public function closePosition($symbol)
    {
        $symbol = strtoupper($symbol);
        
        $position = Position::where('symbol', $symbol)->where('asset_balance', '>', 0)->latest()->first();
        if (!$position) {
            return response()->json(['message' => 'No active position found in database for ' . $symbol], 404);
        }

        $apiKey = env('BINANCE_API_KEY');
        $apiSecret = env('BINANCE_API_SECRET');
        
        if (!$apiKey || !$apiSecret) {
            return response()->json(['message' => 'Binance API credentials missing'], 500);
        }
        
        $side = $position->position_direction === 'LONG' ? 'SELL' : 'BUY';
        
        $timestamp = round(microtime(true) * 1000);
        $queryString = "symbol={$symbol}&side={$side}&type=MARKET&reduceOnly=true&timestamp={$timestamp}";
        $signature = hash_hmac('sha256', $queryString, $apiSecret);
        
        try {
            $response = Http::withHeaders([
                'X-MBX-APIKEY' => $apiKey
            ])->post("https://testnet.binancefuture.com/fapi/v1/order?{$queryString}&signature={$signature}");
            
            if ($response->successful()) {
                $position->decision = 'MANUAL_CLOSE';
                $position->asset_balance = 0;
                $position->save();
                
                return response()->json(['message' => 'Position closed successfully', 'data' => $response->json()]);
            } else {
                return response()->json([
                    'message' => 'Failed to close position on Binance',
                    'error' => $response->json()
                ], $response->status());
            }
        } catch (\Exception $e) {
            return response()->json(['message' => 'Error communicating with Binance API', 'error' => $e->getMessage()], 500);
        }
    }
}
