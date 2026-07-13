<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class ActiveSymbol extends Model
{
    protected $connection = 'quant_shared';
    protected $table = 'active_symbols';
    protected $fillable = ['symbol', 'is_active'];
}
