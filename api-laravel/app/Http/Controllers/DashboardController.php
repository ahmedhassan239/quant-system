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
        try {
            $logs = BotLog::orderBy('created_at', 'desc')->take(100)->get();
            return response()->json(array_reverse($logs->toArray()));
        } catch (\Exception $e) {
            \Illuminate\Support\Facades\Log::error("DashboardController logs error: " . $e->getMessage());
            return response()->json([]);
        }
    }

    public function getDashboardMetrics()
    {
        try {
            // 1 & 2. Total PNL & Win Rate from CLOSED trades (local DB query)
            $totalPnl = 0.00;
            $winningTrades = 0;
            $actualTotalTrades = 0;
            $winRate = 0;

            try {
                $totalPnl = (float)\Illuminate\Support\Facades\DB::table('trade_history')->sum('pnl_usd'); 
                $winningTrades = \Illuminate\Support\Facades\DB::table('trade_history')->where('outcome', 'WIN')->count();
                $actualTotalTrades = \Illuminate\Support\Facades\DB::table('trade_history')->count();
                $winRate = $actualTotalTrades > 0 ? round(($winningTrades / $actualTotalTrades) * 100, 2) : 0;
            } catch (\Exception $e) {
                // Table might not exist or DB busy
            }

            // 3. Wallet Balance strictly from Local Database (NO synchronous external API calls)
            $walletBalance = 0.00;
            $totalUnrealizedProfit = 0.00;
            $totalMarginBalance = 0.00;

            // Query wallet_balance table first
            try {
                $wbRecord = \Illuminate\Support\Facades\DB::table('wallet_balance')->first();
                if ($wbRecord && isset($wbRecord->balance)) {
                    $walletBalance = (float) $wbRecord->balance;
                }
            } catch (\Exception $e) {
                // Ignore missing table
            }

            // Fallback wallet balance from positions table if wallet_balance was 0
            if ($walletBalance <= 0) {
                try {
                    $latestPortfolioRecord = \Illuminate\Support\Facades\DB::table('positions')
                        ->whereNotNull('usdt_balance')
                        ->orderBy('id', 'desc')
                        ->first();
                    if ($latestPortfolioRecord && isset($latestPortfolioRecord->usdt_balance)) {
                        $walletBalance = (float) $latestPortfolioRecord->usdt_balance;
                    }
                } catch (\Exception $e) {
                    // Ignore
                }
            }

            // 4. Active Positions directly from LOCAL DATABASE
            $mappedPositions = collect([]);
            try {
                $latestSubquery = \Illuminate\Support\Facades\DB::table('positions')
                    ->select('symbol', \Illuminate\Support\Facades\DB::raw('MAX(id) as max_id'))
                    ->groupBy('symbol');

                $activeLocalPositions = \Illuminate\Support\Facades\DB::table('positions as p')
                    ->joinSub($latestSubquery, 'latest', function ($join) {
                        $join->on('p.symbol', '=', 'latest.symbol')
                             ->on('p.id', '=', 'latest.max_id');
                    })
                    ->where('p.asset_balance', '>', 0)
                    ->whereNotIn('p.decision', ['CLOSE_LONG', 'CLOSE_SHORT', 'MANUAL_CLOSE', 'SL_CLOSE', 'TP_CLOSE'])
                    ->get();

                $mappedPositions = $activeLocalPositions->map(function ($localPos) {
                    $formatPrice = function($price) {
                        $formatted = number_format((float)$price, 5, '.', '');
                        return preg_replace('/(\.\d{2,}?)0+$/', '$1', $formatted);
                    };

                    $direction = $localPos->position_direction ?: ($localPos->decision === 'SHORT' ? 'SHORT' : 'LONG');
                    $entryPrice = (float)($localPos->average_entry_price ?: $localPos->current_price);
                    $currentPrice = (float)($localPos->current_price ?: $entryPrice);
                    $assetBalance = (float)($localPos->asset_balance ?: 0);
                    $allocatedUsdt = (float)($localPos->allocated_margin ?: 0);

                    $unrealizedPnl = (float)($localPos->pnl_usd ?? 0.0);
                    if ($localPos->pnl_usd === null && $entryPrice > 0 && $assetBalance > 0) {
                        if ($direction === 'LONG') {
                            $unrealizedPnl = ($currentPrice - $entryPrice) * $assetBalance;
                        } else {
                            $unrealizedPnl = ($entryPrice - $currentPrice) * $assetBalance;
                        }
                    }

                    $rawSl = $localPos->stop_loss ?: ($localPos->stop_loss_price ?: null);
                    $actualStopLoss = 'Inactive';
                    if ($rawSl && (float)$rawSl > 0) {
                        $actualStopLoss = $formatPrice((float)$rawSl);
                    }

                    return [
                        'id' => $localPos->id,
                        'symbol' => $localPos->symbol,
                        'direction' => $direction,
                        'entry_price' => $formatPrice($entryPrice),
                        'current_price' => $formatPrice($currentPrice),
                        'unrealized_pnl' => number_format($unrealizedPnl, 2, '.', ''),
                        'allocated_usdt' => number_format($allocatedUsdt, 2, '.', ''),
                        'entry_reason' => $localPos->entry_reason ?: 'Automated Strategy',
                        'stop_loss' => $actualStopLoss,
                        'strategy' => $localPos->strategy ?? 'N/A',
                    ];
                })->values();

                if ($mappedPositions->isNotEmpty()) {
                    $totalUnrealizedProfit = $mappedPositions->sum(function ($pos) {
                        return (float)$pos['unrealized_pnl'];
                    });
                }
            } catch (\Exception $e) {
                \Illuminate\Support\Facades\Log::error("DashboardController active positions error: " . $e->getMessage());
            }

            $totalMarginBalance = $walletBalance + $totalUnrealizedProfit;

            return response()->json([
                'wallet_balance' => number_format($walletBalance, 2, '.', ''),
                'active_unrealized_pnl' => number_format($totalUnrealizedProfit, 2, '.', ''),
                'realized_pnl' => number_format($totalPnl, 2, '.', ''),
                'total_margin_balance' => number_format($totalMarginBalance, 2, '.', ''),
                'win_rate' => $winRate,
                'active_positions' => $mappedPositions
            ]);
        } catch (\Exception $e) {
            \Illuminate\Support\Facades\Log::error("getDashboardMetrics Error: " . $e->getMessage());
            return response()->json([
                'wallet_balance' => '0.00',
                'active_unrealized_pnl' => '0.00',
                'realized_pnl' => '0.00',
                'total_margin_balance' => '0.00',
                'win_rate' => 0,
                'active_positions' => []
            ], 200);
        }
    }

    public function macroTrends()
    {
        try {
            // Only return macro trends for symbols currently active in the scanner
            $activeSymbols = ActiveSymbol::where('is_active', true)->pluck('symbol');
            $trends = MacroState::whereIn('symbol', $activeSymbols)->get();
            return response()->json($trends);
        } catch (\Exception $e) {
            \Illuminate\Support\Facades\Log::error("macroTrends Error: " . $e->getMessage());
            return response()->json([], 200);
        }
    }

    public function symbols()
    {
        try {
            return response()->json(ActiveSymbol::where('is_active', true)->get());
        } catch (\Exception $e) {
            \Illuminate\Support\Facades\Log::error("symbols Error: " . $e->getMessage());
            return response()->json([], 200);
        }
    }

    public function addSymbol(Request $request)
    {
        try {
            $validated = $request->validate([
                'symbol' => 'required|string|unique:active_symbols,symbol'
            ]);

            $symbol = ActiveSymbol::create([
                'symbol' => strtoupper($validated['symbol']),
                'is_active' => true
            ]);

            return response()->json(['message' => 'Symbol added successfully', 'data' => $symbol]);
        } catch (\Illuminate\Validation\ValidationException $ve) {
            return response()->json(['message' => $ve->getMessage(), 'errors' => $ve->errors()], 422);
        } catch (\Exception $e) {
            \Illuminate\Support\Facades\Log::error("addSymbol Error: " . $e->getMessage());
            return response()->json(['message' => 'Failed to add symbol', 'error' => $e->getMessage()], 200);
        }
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
            // 0. Cancel all open orders for this symbol (prevents -2022 ReduceOnly rejection from SL/TP locks)
            $cancelParams = [
                'symbol' => $symbol,
                'timestamp' => number_format(microtime(true) * 1000, 0, '.', '')
            ];
            $cancelQuery = http_build_query($cancelParams, '', '&');
            $cancelSignature = hash_hmac('sha256', $cancelQuery, $apiSecret);
            $cancelUrl = "https://testnet.binancefuture.com/fapi/v1/allOpenOrders?{$cancelQuery}&signature={$cancelSignature}";

            $chCancel = curl_init();
            curl_setopt($chCancel, CURLOPT_URL, $cancelUrl);
            curl_setopt($chCancel, CURLOPT_CUSTOMREQUEST, "DELETE");
            curl_setopt($chCancel, CURLOPT_RETURNTRANSFER, true);
            curl_setopt($chCancel, CURLOPT_CONNECTTIMEOUT, 3);
            curl_setopt($chCancel, CURLOPT_TIMEOUT, 5);
            curl_setopt($chCancel, CURLOPT_HTTPHEADER, ['X-MBX-APIKEY: ' . $apiKey]);
            curl_exec($chCancel);
            curl_close($chCancel);

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
            curl_setopt($chRisk, CURLOPT_CONNECTTIMEOUT, 3);
            curl_setopt($chRisk, CURLOPT_TIMEOUT, 5);
            curl_setopt($chRisk, CURLOPT_HTTPHEADER, ['X-MBX-APIKEY: ' . $apiKey]);
            $riskResult = curl_exec($chRisk);
            curl_close($chRisk);

            $positionAmt = 0;
            $riskData = json_decode($riskResult, true);
            if (is_array($riskData) && count($riskData) > 0) {
                foreach ($riskData as $risk) {
                    if (isset($risk['positionAmt']) && (float)$risk['positionAmt'] != 0) {
                        $positionAmt = (float)$risk['positionAmt'];
                        break;
                    }
                }
            }

            if ($positionAmt == 0) {
                // Fallback to DB quantity
                $positionAmt = $position->position_direction === 'LONG'
                    ? (float)$position->asset_balance
                    : -(float)$position->asset_balance;
            }

            // 2. Determine side
            $side = $positionAmt > 0 ? 'SELL' : 'BUY';
            $qtyFloat = abs($positionAmt);

            // 3. Fetch quantityPrecision from Binance exchangeInfo (public endpoint, no signature needed)
            $chInfo = curl_init();
            curl_setopt($chInfo, CURLOPT_URL, "https://testnet.binancefuture.com/fapi/v1/exchangeInfo");
            curl_setopt($chInfo, CURLOPT_RETURNTRANSFER, true);
            curl_setopt($chInfo, CURLOPT_CONNECTTIMEOUT, 3);
            curl_setopt($chInfo, CURLOPT_TIMEOUT, 5);
            $infoResult = curl_exec($chInfo);
            curl_close($chInfo);

            $quantityPrecision = 0; // safe fallback (whole numbers)
            $exchangeInfo = json_decode($infoResult, true);
            if (isset($exchangeInfo['symbols'])) {
                foreach ($exchangeInfo['symbols'] as $sym) {
                    if ($sym['symbol'] === $symbol) {
                        $quantityPrecision = $sym['quantityPrecision'];
                        break;
                    }
                }
            }

            // 4. Truncate quantity to exact allowed precision (floor, never round up)
            $factor = pow(10, $quantityPrecision);
            $truncatedQty = floor($qtyFloat * $factor) / $factor;
            $exactQuantity = number_format($truncatedQty, $quantityPrecision, '.', '');

            // 5. Prepare parameters strictly as strings
            $timestamp = number_format(microtime(true) * 1000, 0, '.', '');
            $params = [
                'symbol' => $symbol,
                'side' => $side,
                'type' => 'MARKET',
                'quantity' => $exactQuantity,
                'reduceOnly' => 'true',
                'timestamp' => $timestamp
            ];

            // Safety: ensure closePosition is never present
            unset($params['closePosition']);
            
            // 6. Build exact query
            $queryString = http_build_query($params, '', '&');
            
            // 7. Hash signature
            $signature = hash_hmac('sha256', $queryString, $apiSecret);
            
            // 8. Append signature to URL
            $url = "https://testnet.binancefuture.com/fapi/v1/order?{$queryString}&signature={$signature}";

            // 9. Log the final URL for audit
            \Log::info("Binance Close URL: " . $url);
            
            // 10. Execute raw cURL
            $ch = curl_init();
            curl_setopt($ch, CURLOPT_URL, $url);
            curl_setopt($ch, CURLOPT_POST, true);
            curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
            curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 3);
            curl_setopt($ch, CURLOPT_TIMEOUT, 5);
            curl_setopt($ch, CURLOPT_HTTPHEADER, ['X-MBX-APIKEY: ' . $apiKey]);
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
