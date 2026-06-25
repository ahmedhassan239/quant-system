<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;

class TradeLog extends Model
{
    use HasFactory;

    protected $guarded = [];

    // الكاستينج ده مهم جداً عشان Laravel يقرأ الـ JSONB كـ Array مباشرة
    protected $casts = [
        'rationale' => 'array',
    ];
}