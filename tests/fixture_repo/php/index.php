<?php
require_once __DIR__ . '/src/User.php';

use App\Models\User;

function helper(int $x): int
{
    return $x + 1;
}

$u = new User('x');
echo $u->name();
echo helper(1);
