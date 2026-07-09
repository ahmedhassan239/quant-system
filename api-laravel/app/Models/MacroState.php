<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class MacroState extends Model
{
    protected $connection = 'quant_shared';
    protected $table = 'macro_state';
    public $timestamps = false;
}
