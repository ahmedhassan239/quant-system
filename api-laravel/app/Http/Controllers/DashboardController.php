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
        $totalUnrealizedProfit = 0.00;
        $totalMarginBalance = 0.00;

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
                    if (isset($account['totalUnrealizedProfit'])) {
                        $totalUnrealizedProfit = (float) $account['totalUnrealizedProfit'];
                    }
                    if (isset($account['totalMarginBalance'])) {
                        $totalMarginBalance = (float) $account['totalMarginBalance'];
                    }
                }
            } catch (\Exception $e) {
                $walletBalance = 0.00;
                $totalUnrealizedProfit = 0.00;
                $totalMarginBalance = 0.00;
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
                'entry_reason' => $pos->entry_reason,
                'stop_loss' => $pos->stop_loss ? number_format((float)$pos->stop_loss, 2, '.', '') : 'N/A',
                'strategy' => $pos->strategy
            ];
        });

        return response()->json([
            'wallet_balance' => number_format($walletBalance, 2, '.', ''),
            'total_pnl' => number_format($totalUnrealizedProfit, 2, '.', ''),
            'total_margin_balance' => number_format($totalMarginBalance, 2, '.', ''),
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
        
        $position = Position::where('symbol', $symbol)
                            ->where('asset_balance', '>', 0)
                            ->orderBy('id', 'desc')
                            ->first();
        if (!$position) {
            return response()->json(['message' => 'No active position found in database for ' . $symbol], 404);
        }

        $apiKey = env('BINANCE_API_KEY');
        $apiSecret = env('BINANCE_API_SECRET');
        
        if (!$apiKey || !$apiSecret) {
            return response()->json(['message' => 'Binance API credentials missing'], 500);
        }
        
        try {
            // 1. Fetch exact positionAmt from Binance using raw cURL
            $timestamp = number_format(microtime(true) * 1000, 0, '.', '');
            $riskParams = [
                'symbol' => $symbol,
                'timestamp' => $timestamp
            ];
            $riskQueryString = http_build_query($riskParams, '', '&');
            $riskSignature = hash_hmac('sha256', $riskQueryString, $apiSecret);
            $riskUrl = "https://testnet.binancefuture.com/fapi/v2/positionRisk?{$riskQueryString}&signature={$riskSignature}";

            $chRisk = curl_init();
            curl_setopt($chRisk, CURLOPT_URL, $riskUrl);
            curl_setopt($chRisk, CURLOPT_RETURNTRANSFER, true);
            curl_setopt($chRisk, CURLOPT_HTTPHEADER, ['X-MBX-APIKEY: ' . $apiKey]);
            $riskResult = curl_exec($chRisk);
            curl_close($chRisk);

            $positionAmt = 0;
            $riskData = json_decode($riskResult, true);
            if (is_array($riskData) && count($riskData) > 0) {
                foreach ($riskData as $risk) {
                    if (isset($risk['positionAmt']) && abs((float)$risk['positionAmt']) > 0) {
                        $positionAmt = (float)$risk['positionAmt'];
                        break;
                    }
                }
            }

            if ($positionAmt == 0) {
                // If position is 0 on Binance, fallback to DB quantity
                $positionAmt = $position->position_direction === 'LONG' ? $position->asset_balance : -$position->asset_balance;
            }

            // 2. Prepare parameters strictly as strings
            $side = $positionAmt > 0 ? 'SELL' : 'BUY';
            $quantity = abs($positionAmt);
            
            $timestamp = number_format(microtime(true) * 1000, 0, '.', '');
            $params = [
                'symbol' => $symbol,
                'side' => $side,
                'type' => 'MARKET',
                'quantity' => (string)$quantity,
                'reduceOnly' => 'true',
                'timestamp' => $timestamp
            ];
            
            // 3. Build exact query
            $queryString = http_build_query($params, '', '&');
            
            // 4. Hash signature
            $signature = hash_hmac('sha256', $queryString, env('BINANCE_API_SECRET'));
            
            // 5. Append signature to URL
            $url = "https://testnet.binancefuture.com/fapi/v1/order?{$queryString}&signature={$signature}";
            
            // 6. Execute raw cURL
            $ch = curl_init();
            curl_setopt($ch, CURLOPT_URL, $url);
            curl_setopt($ch, CURLOPT_POST, true);
            curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
            curl_setopt($ch, CURLOPT_HTTPHEADER, ['X-MBX-APIKEY: ' . env('BINANCE_API_KEY')]);
            $result = curl_exec($ch);
            $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
            curl_close($ch);

            $responseData = json_decode($result, true);

            if ($httpCode >= 200 && $httpCode < 300) {
                $position->decision = 'MANUAL_CLOSE';
                $position->asset_balance = 0;
                $position->save();
                
                return response()->json(['message' => 'Position closed successfully', 'data' => $responseData]);
            } else {
                $binanceMessage = $responseData['msg'] ?? 'Unknown Binance Error';
                
                return response()->json([
                    'message' => $binanceMessage,
                    'error' => $responseData
                ], $httpCode ?: 400);
            }
        } catch (\Exception $e) {
            return response()->json(['message' => 'Error communicating with Binance API: ' . $e->getMessage()], 500);
        }
    }
}
