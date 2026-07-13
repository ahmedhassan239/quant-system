<?php

namespace App\Http\Controllers;

use Illuminate\Http\Request;
use App\Models\ActiveSymbol;
use App\Models\MacroState;
use App\Models\Position;
use Illuminate\Support\Facades\Http;

class DashboardController extends Controller
{
    public function stats()
    {
        $activePositionsCount = Position::where('asset_balance', '>', 0)->count();
        $totalPnl = Position::sum('pnl_usd'); 
        
        $winningTrades = Position::where('pnl_usd', '>', 0)->count();
        $totalTrades = Position::count() ?: 1;
        $winRate = round(($winningTrades / $totalTrades) * 100, 2);

        // قراءة المفاتيح من ملف .env الأساسي اللي بره فولدر لارافيل
        $apiKey = null;
        $apiSecret = null;
        $envPath = base_path('../.env'); 
        
        if (file_exists($envPath)) {
            $envLines = file($envPath, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
            foreach ($envLines as $line) {
                if (strpos(trim($line), '#') === 0) continue;
                $parts = explode('=', $line, 2);
                if (count($parts) == 2) {
                    $key = trim($parts[0]);
                    $val = trim(trim($parts[1]), "\"'");
                    if ($key === 'BINANCE_API_KEY') $apiKey = $val;
                    if ($key === 'BINANCE_API_SECRET') $apiSecret = $val;
                }
            }
        }

        $walletBalance = 0.00;
        if ($apiKey && $apiSecret) {
            $timestamp = round(microtime(true) * 1000);
            $queryString = "timestamp=" . $timestamp;
            $signature = hash_hmac('sha256', $queryString, $apiSecret);

            try {
                $response = Http::withHeaders([
                    'X-MBX-APIKEY' => $apiKey
                ])->get("https://fapi.binance.com/fapi/v2/balance?{$queryString}&signature={$signature}");

                if ($response->successful()) {
                    $balances = $response->json();
                    foreach ($balances as $asset) {
                        if ($asset['asset'] === 'USDT') {
                            $walletBalance = round((float) $asset['balance'], 2);
                            break;
                        }
                    }
                }
            } catch (\Exception $e) {
                $walletBalance = 0.00;
            }
        }

        return response()->json([
            'total_pnl' => $totalPnl,
            'win_rate' => $winRate,
            'active_positions_count' => $activePositionsCount,
            'wallet_balance' => $walletBalance
        ]);
    }

    public function macroTrends()
    {
        return response()->json(MacroState::all());
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
}
